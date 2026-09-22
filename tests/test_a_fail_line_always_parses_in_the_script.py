"""A FAIL line whose filename was empty or ended in " ::" misled the mIRC
parser (audit L18, #682).

DCCORE FAIL <nick> <chan> <acked> <total> <name> :: <reason>. Only a " :: "
INSIDE the name was defused (to " : : "). An empty name gave "... 0 0  ::
reason" - two spaces - and dccore.mrc collapses runs of spaces, so $6- was
":: reason", $pos found no " :: ", and the window showed the file as
":: reason" and the reason as "failed". A name ending in " ::" gave "name
:: :: reason": the name parsed, the reason showed as ":: reason". An empty
name needs a queue row without a file, a trailing " ::" a non-Windows
filesystem - a real but rare corner.

Every standalone "::" in a name is defused now, wherever it sits, and an
empty name is "?". Asserted through a model of the script's own split -
spaces collapsed, $6-, the first " :: " - so the property is what the
window would show, not the shape of the line.
"""

import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402


def as_the_script_reads_it(line):
    """dccore.mrc's FAIL branch: the leading DCCORE is stripped first
    (`dccore.structured $2-`), $1- collapses runs of spaces, $6- is the
    rest after FAIL nick chan acked total; the first " :: " splits name
    from reason, else the reason is 'failed' and the whole rest is the
    name."""
    words = re.sub(r" +", " ", line).strip().split(" ")
    assert words[0] == "DCCORE"
    rest = " ".join(words[6:])
    marker = rest.find(" :: ")
    if marker < 0:
        return rest, "failed"
    return rest[:marker], rest[marker + 4:]


def fail_line(name, reason="timed out"):
    return adminchat.structured_line("FAIL", {"nick": "n", "channel": "#c", "acked": 0, "total": 0,
                                              "name": name, "reason": reason})


class WhatTheWindowShows(unittest.TestCase):

    def test_an_ordinary_name(self):
        self.assertEqual(as_the_script_reads_it(fail_line("T.flac")), ("T.flac", "timed out"))

    def test_an_empty_name_shows_as_a_question_mark_with_the_reason_intact(self):
        for empty in ("", None, "   "):
            self.assertEqual(as_the_script_reads_it(fail_line(empty)), ("?", "timed out"), repr(empty))

    def test_a_name_ending_in_the_marker(self):
        self.assertEqual(as_the_script_reads_it(fail_line("foo ::")), ("foo : :", "timed out"))

    def test_a_name_that_is_the_marker_or_starts_with_it(self):
        self.assertEqual(as_the_script_reads_it(fail_line("::")), (": :", "timed out"))
        self.assertEqual(as_the_script_reads_it(fail_line(":: foo")), (": : foo", "timed out"))

    def test_a_marker_inside_the_name_still_reads_as_before(self):
        self.assertEqual(as_the_script_reads_it(fail_line("foo :: bar")), ("foo : : bar", "timed out"))

    def test_colons_that_are_not_a_marker_are_untouched(self):
        self.assertEqual(as_the_script_reads_it(fail_line("a::b")), ("a::b", "timed out"))
        self.assertEqual(as_the_script_reads_it(fail_line("Song: Part II")), ("Song: Part II", "timed out"))

    def test_the_model_reads_the_old_lines_the_way_the_audit_said(self):
        """The model itself, checked against the audit's traced results for
        the lines the old code produced."""
        self.assertEqual(as_the_script_reads_it("DCCORE FAIL n #c 0 0  :: timed out"), (":: timed out", "failed"))
        self.assertEqual(as_the_script_reads_it("DCCORE FAIL n #c 0 0 foo :: :: timed out"), ("foo", ":: timed out"))


class TheOtherLinesGetTheSameName(unittest.TestCase):

    def test_an_empty_name_on_every_kind_is_a_question_mark(self):
        for kind in ("REQUEST", "QUEUED", "SENDING", "RESUMED", "SENT"):
            line = adminchat.structured_line(kind, {"nick": "n", "channel": "#c", "name": ""})

            self.assertTrue(line.endswith(" ?"), (kind, line))


if __name__ == "__main__":
    unittest.main()
