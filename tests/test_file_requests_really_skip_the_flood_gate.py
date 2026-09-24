"""#888/#894's exemption, executed rather than read (#897).

#894 fixed #888 by adding `not is_file_request` to the flood gate's `if` in
irc.py, and tested it two ways: `security.is_flooding()` unit tests, and
`test_irc_dispatch.py`'s FloodGateCoverageTests, which lift the two
expressions out of irc.py's source and evaluate them against a corpus of
message strings. Neither one runs the real `if` statement.

That gap is real, not theoretical: removing `not is_file_request` from the
actual line in irc.py - putting the flood gate back exactly as it was
before #888 - left the WHOLE SUITE GREEN. The source-lifting tests still
passed because they evaluate the expressions on their own, off to the
side; the unit tests still passed because they call `security.is_flooding()`
directly and never go near the `if` that is supposed to skip it. The
behaviour the operator asked for had no test that would notice it going.

Since #789, `irc.irc_loop()` can be driven for real against a scripted
server - registered, then fed channel lines - with the threads it starts
recorded rather than run. This drives a burst of real `!<bot> <file>`
lines through it and checks what actually happens: every one dispatches,
none mute the user, and a mixed-in search still does. Removing
`not is_file_request` fails every test below.
"""

import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests import test_the_bots_own_nick_follows_the_server as own  # noqa: E402
from tests.test_what_a_user_typed_reaches_the_log_printable import _Records  # noqa: E402

NICK = "SomeBot"
USER = "listener"
CHANNEL = "#somechannel"


class DrivesFileRequests(own.DrivesPastRegistration):
    """001, then a burst of channel lines from one user, with the threads
    irc_loop() would start recorded rather than run and MAX_REQUESTS turned
    down so a real paste-sized burst crosses it inside the test."""

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME=NICK, ORIGINAL_NICK=NICK, ALT_NICKNAME=NICK + "_",
                        CHANNEL=CHANNEL, MAX_REQUESTS=3, REQUEST_WINDOW=5, MUTE_TIME=30)
        config.muted_until.clear()
        config.banned_users.clear()
        config.user_requests.clear()
        self.addCleanup(config.muted_until.clear)
        self.addCleanup(config.banned_users.clear)
        self.addCleanup(config.user_requests.clear)
        _Records.started = []
        own.irc.threading.Thread = _Records

    def channel_line(self, text, nick=USER):
        return ":%s!~u@%s.example PRIVMSG %s :%s" % (nick, nick.lower(), CHANNEL, text)

    def after_001(self, *lines):
        self.oserve.queued[:] = []
        self.run_registration(own.NOTICE_AUTH, own.welcome(NICK), *lines)

    def download_dispatches(self):
        return [args for target, args in _Records.started
                if target is dcc.handle_download_request]

    def mute_notices(self):
        return [m for _u, m, *_ in self.oserve.queued
                if m.startswith("NOTICE") and "moving too fast" in m]


class APastedBatchOfFileRequestsIsNeverMetered(DrivesFileRequests):

    def test_every_row_of_a_batch_past_max_requests_still_dispatches(self):
        """The reported case: more file requests than MAX_REQUESTS, sent
        together. If the gate still caught them, the later rows would be
        `continue`d before reaching the dispatch below and never dispatch."""
        rows = ["!%s Track%d.flac" % (NICK, n) for n in range(6)]
        self.assertGreater(len(rows), config.MAX_REQUESTS)

        self.after_001(*(self.channel_line(row) for row in rows))

        self.assertEqual(len(self.download_dispatches()), len(rows),
                         "at least one row in the batch was dropped by the flood gate")

    def test_the_user_is_never_muted_by_a_batch(self):
        rows = ["!%s Track%d.flac" % (NICK, n) for n in range(6)]

        self.after_001(*(self.channel_line(row) for row in rows))

        self.assertNotIn(USER.lower(), config.muted_until)
        self.assertEqual(self.mute_notices(), [])

    def test_a_batch_twice_the_limit_still_does_not_mute(self):
        """Not "a little over" - comfortably past it, the way an album pasted
        as fifteen rows against a MAX_REQUESTS of ten was."""
        rows = ["!%s Track%d.flac" % (NICK, n) for n in range(2 * config.MAX_REQUESTS + 1)]

        self.after_001(*(self.channel_line(row) for row in rows))

        self.assertEqual(len(self.download_dispatches()), len(rows))
        self.assertNotIn(USER.lower(), config.muted_until)

    def test_a_file_request_is_served_during_a_mute_earned_by_something_else(self):
        """The other half of the operator's decision: a mute from searching
        does not block the queue a file request would add to."""
        config.muted_until[USER.lower()] = time.time() + 30

        self.after_001(self.channel_line("!%s Track.flac" % NICK))

        self.assertEqual(len(self.download_dispatches()), 1,
                         "a file request was blocked by an unrelated mute")


class THE_CONTROL_SearchesAreStillMetered(DrivesFileRequests):
    """If this class failed, the harness itself would be too permissive to
    prove anything above - a mute that never happens because nothing here
    can trigger one is not evidence the exemption works."""

    def test_enough_searches_still_mute_the_user(self):
        """Exactly one crossing of the limit (MAX_REQUESTS + 1), not more:
        sent this fast, a second crossing would immediately escalate the
        mute into a ban (security.is_flooding()'s own behaviour, not a bug
        in this test's harness) - a real paste can do that too, but it is
        not what this test is checking."""
        rows = ["@find Track%d" % n for n in range(config.MAX_REQUESTS + 1)]

        self.after_001(*(self.channel_line(row) for row in rows))

        self.assertIn(USER.lower(), config.muted_until)
        self.assertEqual(len(self.mute_notices()), 1)

    def test_once_muted_a_search_stops_dispatching(self):
        rows = ["@find Track%d" % n for n in range(config.MAX_REQUESTS + 3)]

        self.after_001(*(self.channel_line(row) for row in rows))

        starts = [target for target, _args in _Records.started]
        # execute_search is threaded per accepted search; muted rows never
        # reach that call at all.
        import list as list_mod
        searches = [t for t in starts if t is list_mod.execute_search]
        self.assertLess(len(searches), len(rows), "the mute never actually stopped anything")


if __name__ == "__main__":
    unittest.main()
