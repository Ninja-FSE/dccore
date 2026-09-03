"""Every document this project points at has to exist.

The README was cut from 11KB to 4KB by moving installation into
docs/INSTALL.md and the roadmap into docs/FUTURE.md, which turned it into
mostly links. A link to a file that has been renamed or removed is the most
likely way this rots, it is invisible until somebody clicks it, and for a
public repository the README is the first thing anyone reads.

Checked by resolving the paths rather than by reading them, so renaming a doc
without updating its referrers fails here rather than in a stranger's browser.
"""

import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every markdown file we author. docs/UPDATES.md is excluded: it is a changelog
# and its older entries deliberately name files that no longer exist.
AUTHORED = [
    "README.md",
    os.path.join("docs", "INSTALL.md"),
    os.path.join("docs", "FUTURE.md"),
    os.path.join("docs", "ADMIN-CONSOLE.md"),
    os.path.join("docs", "WINDOWS.md"),
    os.path.join("docs", "CONVENTIONS.md"),
]

LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def read(relative):
    with io.open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


class EveryInternalLinkResolves(unittest.TestCase):

    def test_no_document_points_at_a_missing_file(self):
        broken = []
        for relative in AUTHORED:
            base = os.path.dirname(os.path.join(REPO_ROOT, relative))
            for text, target in LINK.findall(read(relative)):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                target = target.split("#", 1)[0]
                if target and not os.path.exists(os.path.join(base, target)):
                    broken.append(f"{relative}: [{text}]({target})")

        self.assertEqual(broken, [], "; ".join(broken))

    def test_the_scan_actually_finds_links(self):
        """Fixture invariant. A regex that matched nothing would pass the test
        above against a README full of dead links."""
        found = sum(len(LINK.findall(read(r))) for r in AUTHORED)

        self.assertGreater(found, 10, "the link scan stopped matching")

    def test_every_authored_document_exists(self):
        missing = [r for r in AUTHORED
                   if not os.path.exists(os.path.join(REPO_ROOT, r))]

        self.assertEqual(missing, [], "; ".join(missing))


class TheRoadmapKeepsItsTwoHalves(unittest.TestCase):
    """FUTURE.md's whole value is the line between what is built and what is
    not. If a section heading is renamed away, the file becomes a wish list."""

    def test_it_separates_implemented_from_planned(self):
        body = read(os.path.join("docs", "FUTURE.md"))

        self.assertIn("## Implemented", body)
        self.assertIn("## Planned", body)

    def test_implemented_comes_first(self):
        """Stated in the file's own instructions, and it is the ordering that
        makes it readable as a status rather than a pitch."""
        body = read(os.path.join("docs", "FUTURE.md"))

        self.assertLess(body.index("## Implemented"), body.index("## Planned"))

    def test_the_unbuilt_features_are_under_planned(self):
        """The two things most likely to be assumed finished, because they have
        been designed and written up in detail."""
        body = read(os.path.join("docs", "FUTURE.md"))
        planned = body[body.index("## Planned"):]

        for feature in ("Multiple lists", "multiple folders per list"):
            with self.subTest(feature=feature):
                self.assertIn(feature, planned)


class TheReadmeStaysAnEntryPoint(unittest.TestCase):

    def test_it_points_at_the_install_guide(self):
        self.assertIn("docs/INSTALL.md", read("README.md"))

    def test_it_points_at_the_roadmap(self):
        """So nobody has to read the feature list and guess which half of it is
        aspirational."""
        self.assertIn("docs/FUTURE.md", read("README.md"))


if __name__ == "__main__":
    unittest.main()
