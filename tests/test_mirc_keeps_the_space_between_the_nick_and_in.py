"""The window printed "SomeNickin #channel" for "SomeNick in #channel".

dccore.in returned " in #channel" built with $chr(32), so the result started with
a space - and mIRC drops leading spaces from what an alias returns, which is why
the nick and the word ran together on every REQUEST, QUEUED, SENDING, SENT, FAIL
and SEARCH line. The two spaces are now non-breaking ($chr(160)), which mIRC does
not treat as spaces, and which the script already uses for its column padding.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def script():
    with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


class TheAlias(unittest.TestCase):

    def line(self):
        return script().split("alias dccore.in {", 1)[1].split("\n", 1)[0]

    def test_the_spaces_around_in_are_non_breaking(self):
        self.assertIn("return $+($chr(160),in,$chr(160),$1)", self.line())

    def test_it_no_longer_starts_with_a_plain_space(self):
        self.assertNotIn("$+($chr(32)", self.line())

    def test_a_channel_less_event_still_prints_nothing(self):
        line = self.line()
        self.assertIn("if ($1 == $null) || ($1 == -) { return }", line)

    def test_the_reason_is_written_down(self):
        text = script()
        self.assertIn("space at the start of what an alias returns is dropped by mIRC", text.replace("\n; ", " "))

    def test_no_alias_returns_a_value_that_starts_with_a_plain_space(self):
        """The same trap anywhere else: `return` of something that begins with
        $chr(32) loses it."""
        for number, line in enumerate(script().split("\n"), 1):
            self.assertNotRegex(line, r"return \$\+\(\$chr\(32\)", f"line {number}: {line.strip()}")

    def test_every_use_joins_the_nick_with_dollar_plus(self):
        """`$2 $+ $dccore.in($3)`: the nick, then the alias's own spaces."""
        uses = re.findall(r"(\S+ \$\+ \$dccore\.in\(\$3\))", script())
        self.assertGreaterEqual(len(uses), 7)
        for use in uses:
            self.assertRegex(use, r"^\$\d \$\+ \$dccore\.in\(\$3\)$")

    def test_what_the_line_reads_like(self):
        """Simulate the join: nick, then the alias's result, then the rest."""
        nbsp = "\xa0"
        nick, channel = "SomeNick", "#example"
        result = nbsp + "in" + nbsp + channel
        line = nick + result + " asked for a file"
        self.assertEqual(line.replace(nbsp, " "), "SomeNick in #example asked for a file")
        self.assertNotIn("SomeNickin", line.replace(nbsp, " "))


if __name__ == "__main__":
    unittest.main()
