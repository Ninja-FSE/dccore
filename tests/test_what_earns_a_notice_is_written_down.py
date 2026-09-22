"""Notices were a hand-picked list of call sites, and the help text
promised disconnects among them while none existed (audit L44, #708).

record_notice() is reached only through send_debug(notice=...), by the
call sites that know they are one - nine of them, for list-rebuild
failures, kick and rejoin, and join refusals. The operator-facing help for
NOTICES_FILE said "kicks, failed rebuilds, disconnects", and the reconnect
block only print()ed: the bot's link dropped nightly, the dashboard badge
stayed clear, and the operator reading the help assumed no disconnect had
happened. The split relied on every future author remembering notice=,
with no list of the intended events anywhere.

announce.NOTICE_EVENTS is that list now - the event, the module, a piece
of the line, the severity - and the disconnect is on it and raises one.
The guard finds each event's emitter by its line and checks it passes
notice= with that severity, and that no other notice= exists that the
list does not name.
"""

import contextlib
import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def source(name):
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def notice_after(text, marker):
    """The notice= argument of the send_debug call whose text holds `marker`:
    the first `notice="..."` within the call's next few hundred characters,
    or None."""
    at = text.index(marker)
    window = text[at:at + 600]
    found = re.search(r'notice="(\w+)"', window)
    return found.group(1) if found else None


class TheListMatchesTheCode(unittest.TestCase):

    def test_every_named_event_passes_notice_with_its_severity(self):
        for event, module, marker, severity in announce.NOTICE_EVENTS:
            with self.subTest(event=event):
                text = source(module)
                self.assertIn(marker, text, "%s: the emitter's line has changed" % event)
                self.assertEqual(notice_after(text, marker), severity, event)

    def test_every_notice_in_the_tree_is_a_named_event(self):
        """The other direction: a notice= nobody wrote down."""
        named = {}
        for _event, module, marker, _severity in announce.NOTICE_EVENTS:
            named.setdefault(module, []).append(marker)
        for module in ("commands.py", "irc.py", "dcc.py", "dcc_fetch.py", "announce.py", "list.py", "webserver.py"):
            text = source(module)
            sites = [m.start() for m in re.finditer(r'notice="(warning|error)"', text)]
            # A comment that mentions notice= is not a call site.
            sites = [s for s in sites if not text[text.rfind(chr(10), 0, s) + 1:s].lstrip().startswith("#")]
            for site in sites:
                before = text[max(0, site - 700):site]
                self.assertTrue(any(marker in before for marker in named.get(module, [])),
                                "%s at %d: a notice= that NOTICE_EVENTS does not name" % (module, site))

    def test_the_severities_are_the_two_the_badge_knows(self):
        for event, _m, _k, severity in announce.NOTICE_EVENTS:
            self.assertIn(severity, announce.NOTICE_SEVERITIES, event)


class TheDisconnectIsOne(DCCoreTestCase):

    def test_the_help_text_names_what_is_recorded(self):
        import settings_help
        help_text = settings_help.PLAIN_HELP["NOTICES_FILE"]

        self.assertIn("a lost connection", help_text)
        self.assertIn("a kick or a refused join", help_text)
        self.assertIn("a failed list rebuild", help_text)
        self.assertNotIn("disconnects", help_text)
        self.assertIn(("the connection was lost", "irc.py", "Lost the connection to the IRC server", "warning"),
                      announce.NOTICE_EVENTS)

    def test_a_lost_connection_records_a_warning(self):
        """The line the reconnect block sends, through the real send_debug:
        the badge sees it even though the channel line waits for the link."""
        self.set_config(DEBUG_CHANNEL="", DEBUG_TO_CONSOLE=False)
        from tests.support import no_disk_writes
        import db
        no_disk_writes(db)

        with contextlib.redirect_stdout(io.StringIO()):
            announce.send_debug("Lost the connection to the IRC server; reconnecting in 10 seconds.",
                                category="INFO", notice="warning")

        unread, _ = announce.unread_notices()
        self.assertGreaterEqual(unread, 1)
        import defaults as config
        latest = list(config.notices)[-1]
        self.assertEqual(latest["severity"], "warning")
        self.assertIn("Lost the connection", latest["text"])


if __name__ == "__main__":
    unittest.main()
