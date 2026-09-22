"""A merge that was "resolved" must not leave its markers in a tracked file.

This project's changelogs take a new entry from nearly every pull request, so
two branches merged in turn conflict in `docs/UPDATES.md` and
`docs/UPDATES-PUBLIC.md` as a matter of course, and each one is resolved by
hand. Twice in one day that went wrong, and nothing noticed either time:

  * a merge resolved on one side left a bare separator line behind, with no
    start or end marker beside it, and it sat in the changelog until a
    review read the diff;
  * a resolution script that handled the two changelogs left the same
    markers in `docs/WINDOWS.md`, committed, with the whole suite green.

A marker is three lines a person can miss and a test never looks for. This
looks: every tracked text file, the changelogs included (they are where it
happens), for a line that is exactly a start marker, an end marker, or the
separator.

The separator alone is what the first case left, so it is checked alone. A
markdown underline of exactly that width is the only other thing that can be
that line, and none exists here; if one is ever wanted, make the heading text
longer or use a dash rule.
"""

import io
import os
import re
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# The identifier sweep's list, not a copy: a suffix missing from it is a
# file nobody scans, and .mrc and .command each shipped unscanned once
# because that list had to be told. One list, one place to tell.
from tests.test_no_personal_identifiers_ship import TEXT_SUFFIXES  # noqa: E402

# Built from parts so this file does not contain a marker itself.
START = "<" * 7
END = ">" * 7
SEPARATOR = "=" * 7
MARKER = re.compile(
    r"^(?:" + START + r"(?: .*)?|" + END + r"(?: .*)?|" + SEPARATOR + r")\s*$")


def tracked_text_files():
    try:
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True,
                                capture_output=True, timeout=120)
    except Exception:  # noqa: BLE001 - no git, or not a checkout
        return None
    names = [n for n in listed.stdout.decode("utf-8", "replace").split("\0") if n]
    return [n for n in names if n.endswith(TEXT_SUFFIXES)]


def markers_in(text):
    """(line number, line) for every conflict marker in `text`."""
    return [(number, line) for number, line in enumerate(text.split("\n"), 1)
            if MARKER.match(line)]


class TheMarkerDetector(unittest.TestCase):

    def test_a_start_marker_is_found(self):
        self.assertEqual(markers_in("a\n" + START + " HEAD\nb\n"), [(2, START + " HEAD")])

    def test_an_end_marker_is_found(self):
        self.assertEqual(markers_in("a\n" + END + " origin/main\n"), [(2, END + " origin/main")])

    def test_a_bare_separator_is_found(self):
        """What a half-resolved merge leaves: no start, no end, just this."""
        self.assertEqual(markers_in("entry\n" + SEPARATOR + "\nnext\n"), [(2, SEPARATOR)])

    def test_the_ordinary_rules_in_the_docs_are_not(self):
        """The example list format in the changelog has rules of other widths
        and prose can carry the characters mid-line."""
        text = "=" * 19 + "\n" + "=" * 26 + "\na " + SEPARATOR + " b\n" + "-" * 7 + "\n"
        self.assertEqual(markers_in(text), [])


class NothingTrackedCarriesOne(unittest.TestCase):

    def test_no_tracked_text_file_has_a_conflict_marker(self):
        files = tracked_text_files()
        if files is None:
            self.skipTest("could not list the tracked files")
        self.assertGreater(len(files), 50, "the file list came back nearly empty")

        found = {}
        for name in files:
            with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8",
                         errors="replace") as handle:
                hits = markers_in(handle.read())
            if hits:
                found[name] = [number for number, _line in hits]

        self.assertEqual(found, {},
                         "conflict markers left in tracked files (file: lines) - "
                         "a merge was committed unresolved")


if __name__ == "__main__":
    unittest.main()
