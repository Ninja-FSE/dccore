"""The @DCCore window's button lights up like a channel's (#892).

Every other mIRC window's button turns red when something new is said in
it; @DCCore never did. Every line went through dccore.echo, a plain
`echo -ti2` - an EVENT line in mIRC's terms. `-m` "treats line as user
message", which is what gives a button the message colour.

Activity now takes the message colour (dccore.msg), a failure the
highlight colour (dccore.alert, `/window -g2`), and what a channel would
also treat as an event - the STATUS heartbeat, joins, parts, bans - stays
an event line. mIRC cannot run here, so this reads the script's code
lines, comments stripped, so a comment naming a switch cannot satisfy it.
"""

import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.test_the_bots_window_in_mirc import code_lines, script_text  # noqa: E402


def alias_body(name):
    """The code lines between `alias <name> {` and its closing brace."""
    lines = code_lines(script_text())
    start = lines.index("alias %s {" % name)
    body = []
    for line in lines[start + 1:]:
        if line == "}":
            return body
        body.append(line.strip())
    raise AssertionError("alias %s never closes" % name)


def renderer_of(tag):
    """Which alias draws the line tagged `tag` in the feed handler."""
    for line in code_lines(script_text()):
        match = re.match(r"\s*(dccore\.\w+) \$dccore\.tag\(%s," % re.escape(tag), line)
        if match:
            return match.group(1)
    raise AssertionError("no line draws the %s tag" % tag)


class WhichLineTakesWhichColour(unittest.TestCase):

    def test_activity_takes_the_message_colour(self):
        for tag in ("REQUEST", "QUEUED", "SENDING", "RESUMED", "SENT", "SEARCH", "LISTS"):
            with self.subTest(tag=tag):
                self.assertEqual(renderer_of(tag), "dccore.msg")

    def test_a_failure_takes_the_highlight_colour(self):
        for tag in ("FAILED", "DROPPED"):
            with self.subTest(tag=tag):
                self.assertEqual(renderer_of(tag), "dccore.alert")

    def test_the_heartbeat_stays_an_event(self):
        """Every few minutes: as a message it would keep the button red
        always, and a colour that is always on says nothing."""
        self.assertEqual(renderer_of("STATUS"), "dccore.echo")

    def test_joins_parts_and_bans_stay_events(self):
        """The LOG handler, which draws them, is left on dccore.echo - as a
        channel treats a join or a mode change."""
        self.assertIn("dccore.echo $dccore.tag(%cat,%group) $3-",
                      [line.strip() for line in code_lines(script_text())])


class TheAliasesDoWhatTheySay(unittest.TestCase):

    def test_the_message_line_is_echo_m_on_mirc_7(self):
        body = alias_body("dccore.msg")
        self.assertIn("if ($version >= 7) { echo -mti2 $dccore.win "
                      "$iif($1- == $null,$dccore.nbsp,$1-) }", body)
        self.assertIn("else { echo -ti2 $dccore.win $iif($1- == $null,$dccore.nbsp,$1-) }", body,
                      "an older mIRC must still get the line, as before")

    def test_the_alert_highlights_the_button_but_not_the_one_you_are_reading(self):
        body = alias_body("dccore.alert")
        self.assertEqual(body[0], "dccore.msg $1-", "the line itself is shown first")
        self.assertIn("if (($version >= 7) && ($active != $dccore.win)) { window -g2 $dccore.win }", body)

    def test_the_event_line_is_unchanged(self):
        self.assertEqual(alias_body("dccore.echo"),
                         ["dccore.window", "echo -ti2 $dccore.win $iif($1- == $null,$dccore.nbsp,$1-)"])


if __name__ == "__main__":
    unittest.main()
