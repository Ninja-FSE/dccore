"""The mIRC docs and script required "a bot of 1.13 or later" while the
tree was v1.12.2 (audit L2, #666).

`docs/ADMIN-CONSOLE.md` and the header, `/dccore version` text and two
comments of `scripts/mirc/dccore.mrc` named 1.13 as the bot the script
needs - a release that had not been cut, from a tree that called itself
1.12.2. An operator who read the requirement and checked their bot's
version would conclude the script could not work with it. The claims now
say what they mean: a bot that answers `hello`, the DCCore the script ships
with or a later one. The release roll, not a fix, is what may name a number.

The guard reads the shipped prose: no "DCCore x.y", "bot of x.y", "older
than x.y", "before x.y", "since x.y" or "from x.y" in the operator docs or
the script may name a version the tree has not reached.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults  # noqa: E402

# The prose that states a requirement or a history: a version right after
# these words is a claim about the bot. "protocol 1.1" and "mIRC 6.10" are
# not preceded by any of them.
VERSION_CLAIM = re.compile(
    r"(?:DCCore v?|bot of |older than |before (?:the )?|since |from )v?(\d+)\.(\d+)(?:\.(\d+))?\b")

# The operator-facing text that ships in the tree. The changelogs and the
# roadmap legitimately name versions that are still to come.
SHIPPED_PROSE = [
    "README.md",
    "docs/ADMIN-CONSOLE.md",
    "docs/INSTALL.md",
    "docs/WINDOWS.md",
    "docs/MACOS.md",
    "docs/CONVENTIONS.md",
    "scripts/mirc/dccore.mrc",
]


def tree_version():
    match = re.search(r"v(\d+)\.(\d+)(?:\.(\d+))?", defaults.SCRIPT_VERSION)
    return tuple(int(part or 0) for part in match.groups())


def version_claims(text):
    return [(tuple(int(part or 0) for part in m.groups()), m.group(0)) for m in VERSION_CLAIM.finditer(text)]


class TheShippedProse(unittest.TestCase):

    def claims_in(self, relative):
        path = os.path.join(REPO_ROOT, relative)
        if not os.path.exists(path):
            return []
        with io.open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        found = []
        for number, line in enumerate(lines, 1):
            for version, phrase in version_claims(line):
                found.append((version, "%s:%d: %s" % (relative, number, phrase)))
        return found

    def test_no_doc_or_script_names_a_bot_the_tree_has_not_reached(self):
        reached = tree_version()
        ahead = [where for relative in SHIPPED_PROSE
                 for version, where in self.claims_in(relative) if version > reached]

        self.assertEqual(ahead, [], "requires a bot newer than %s: %s" % (defaults.SCRIPT_VERSION, ahead))

    def test_the_script_asks_for_a_bot_that_answers_hello_instead(self):
        with io.open(os.path.join(REPO_ROOT, "scripts/mirc/dccore.mrc"), encoding="utf-8") as handle:
            script = handle.read()

        self.assertIn("A bot that answers `hello` - the DCCore this script ships with", script)
        self.assertIn("for the DCCore it ships with and later.", script)

    def test_the_console_doc_asks_for_a_bot_that_answers_hello_instead(self):
        with io.open(os.path.join(REPO_ROOT, "docs/ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            doc = handle.read()

        self.assertIn("a bot that answers\n`hello`: the DCCore this script ships with, or a later one.", doc)


class TheClaimPattern(unittest.TestCase):
    """The regex reads what the audit found and leaves the rest alone."""

    def test_it_reads_the_phrases_the_audit_found(self):
        for phrase in ["a bot of 1.13 or later", "DCCore 1.13 or later on the bot",
                       "for DCCore 1.13 and later", "older than 1.13", "before 1.13's release",
                       "before the 1.13 release"]:
            self.assertEqual([v for v, _p in version_claims(phrase)], [(1, 13, 0)], phrase)

    def test_it_leaves_the_protocol_and_mirc_versions_alone(self):
        self.assertEqual(version_claims("DCCORE HELLO 1.1 <botnick>"), [])
        self.assertEqual(version_claims("mIRC 6.10 or later"), [])
        self.assertEqual(version_claims("protocol 1.1, for a bot"), [])

    def test_the_tree_version_is_read_from_defaults(self):
        self.assertEqual(len(tree_version()), 3)
        self.assertGreaterEqual(tree_version(), (1, 12, 2))


if __name__ == "__main__":
    unittest.main()
