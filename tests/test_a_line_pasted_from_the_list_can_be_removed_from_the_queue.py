"""`@<nick>-remove <file>` with a line copied from the list removes that file (#1473).

A request strips the "  ::INFO:: <size> ..." tail before it queues the row
(#234), and the row's name is the base name of the path the request opens.
handle_queue_remove_file() compared the whole pasted line, tail included,
so nothing matched: the user was told the file "is not in your queue", and
it was sent anyway. The same went for a name with its folder in front.

Now dcc.queued_name_of_request() works out, from the text alone, the name
the same text is queued under, and the remove matches with that. The first
class drives a real request through dcc.handle_download_request() and then
the real remove; the others pin the tail variants, what did not change, the
IRC and CTCP routes in, and the function itself.

Every nick, channel and file name here is invented.
"""

import contextlib
import io
import ntpath
import os
import sys
import threading
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests.test_path_security import InlineThread  # noqa: E402
from tests.test_webserver import write_master_list  # noqa: E402

OTHER = feed.OTHER
NICK = "DCCoreTest"          # feed.ServesARealRequest's NICKNAME
USER = "someuser"

LOVE = "Some Artist - 13 - Love.flac"
PASTED = LOVE + "  ::INFO:: 36.82MB 3m1s 1681/44.1/S"
KEEP = "Some Artist - 14 - Other.flac"


class InlineIrcThread:
    """irc.py's threading.Thread, run on the spot: the handler it starts is
    the unit under test here, so it runs rather than being recorded."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.kwargs = target, tuple(args), kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class Library(feed.ServesARealRequest):
    """A library and a master list with the two tracks, the slots full so
    every request queues, and the real request and remove driven."""

    def setUp(self):
        super().setUp()
        for name in (LOVE, KEEP):
            with io.open(os.path.join(self.tree.music, name), "wb") as handle:
                handle.write(b"\x00" * 4096)
        album = os.path.join(self.tree.music, "Album")
        os.makedirs(album, exist_ok=True)
        with io.open(os.path.join(album, "track.flac"), "wb") as handle:
            handle.write(b"\x00" * 4096)
        write_master_list(self.tree.lists, NICK, [
            (None, [(LOVE, "36.82MB 3m1s 1681/44.1/S"), (KEEP, "4KB")]),
            ("Album", [("track.flac", "4KB")]),
        ])
        config.channel_users[OTHER] = {USER}
        self.fill_the_slots()

    def rows(self, user=USER):
        return [row["file"] for row in config.dcc_queue.get(user, [])]

    def notices(self, user=USER):
        return [m for who, m, *_ in self.oserve.queued if who == user]

    def ask(self, what):
        self.request(what, channel=OTHER, user=USER)

    def remove(self, what):
        """The real handler; what it printed comes back."""
        self.oserve.queued.clear()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.handle_queue_remove_file(self.sock, USER, OTHER, what)
        return out.getvalue()


class APastedLineEndToEnd(Library):

    def test_the_request_queues_the_bare_name(self):
        """Fixture invariant: what the remove has to find."""
        self.ask(PASTED)
        self.assertEqual(self.rows(), [LOVE])

    def test_the_same_pasted_line_removes_that_row_only(self):
        self.ask(PASTED)
        self.ask(KEEP)
        self.assertEqual(self.rows(), [LOVE, KEEP])

        self.remove(PASTED)

        self.assertEqual(self.rows(), [KEEP])

    def test_the_notice_names_the_bare_file(self):
        self.ask(PASTED)
        self.ask(KEEP)

        self.remove(PASTED)

        told = self.notices()
        self.assertEqual(len(told), 1, told)
        self.assertIn('Removed "%s" from your queue.' % LOVE, told[0])
        self.assertNotIn("::INFO::", told[0])
        self.assertNotIn("36.82MB", told[0])

    def test_the_log_names_the_bare_file(self):
        self.ask(PASTED)
        self.ask(KEEP)

        printed = self.remove(PASTED)

        self.assertIn("[COMMANDS] %s removed 1 queued file(s) matching %r." % (USER, LOVE), printed)
        self.assertNotIn("::INFO::", printed)

    def test_the_row_left_is_the_one_that_is_sent(self):
        self.ask(PASTED)
        self.ask(KEEP)
        self.remove(PASTED)

        InlineThread.dispatched = []
        self.free_the_slots()
        self.quietly(dcc.check_queue_and_send, USER)

        sent = [args[3] for name, args in InlineThread.dispatched if name == "start_dcc_send"]
        self.assertEqual(sent, [KEEP])

    def test_without_the_remove_the_pasted_file_would_have_gone_first(self):
        """Discriminator for the test above: the pickup does send LOVE when
        it is still queued, so its absence there is the remove's doing."""
        self.ask(PASTED)
        self.ask(KEEP)

        InlineThread.dispatched = []
        self.free_the_slots()
        self.quietly(dcc.check_queue_and_send, USER)

        sent = [args[3] for name, args in InlineThread.dispatched if name == "start_dcc_send"]
        self.assertEqual(sent[:1], [LOVE])


class ANameWithItsFolderInFront(Library):

    def test_it_is_queued_under_its_base_name(self):
        """Fixture invariant."""
        self.ask("Album/track.flac")
        self.assertEqual(self.rows(), ["track.flac"])

    def test_the_same_text_removes_it(self):
        self.ask("Album/track.flac")
        self.ask(KEEP)

        self.remove("Album/track.flac")

        self.assertEqual(self.rows(), [KEEP])
        self.assertIn('Removed "track.flac" from your queue.', self.notices()[0])

    def test_a_folder_on_its_own_removes_nothing(self):
        self.ask("Album/track.flac")
        self.ask(KEEP)

        for what in ("/", "Album/", "Album"):
            with self.subTest(what=what):
                self.remove(what)
                self.assertEqual(self.rows(), ["track.flac", KEEP])
                # The typed text, not the empty name it reduces to.
                self.assertIn('"%s" is not in your queue.' % what, self.notices()[0])


class EveryTailARequestStrips(Library):
    """Each form strip_info_suffix() takes off a request comes off a remove."""

    TAILS = (
        LOVE + "  ::INFO:: 36.82MB 3m1s 1681/44.1/S",
        LOVE + " ::INFO:: 36.82MB",
        LOVE + "::INFO::36.82MB",
        LOVE + "  ::info:: 36.82mb",
        LOVE + " ::INFO:: 1MB (c) OmeNServE v2.60 (c)",
        LOVE + " ---- 18.8Mb",
    )

    def test_each_one_removes_the_row(self):
        for pasted in self.TAILS:
            with self.subTest(pasted=pasted):
                config.dcc_queue.clear()
                self.ask(LOVE)
                self.ask(KEEP)

                self.remove(pasted)

                self.assertEqual(self.rows(), [KEEP])
                self.assertIn('Removed "%s" from your queue.' % LOVE, self.notices()[0])

    def test_each_one_is_queued_under_the_same_name(self):
        """The request strips them too: one derivation for both."""
        for pasted in self.TAILS:
            with self.subTest(pasted=pasted):
                config.dcc_queue.clear()
                self.ask(pasted)
                self.assertEqual(self.rows(), [LOVE])


class WhatDidNotChange(Library):

    def test_a_bare_name_still_removes(self):
        self.ask(LOVE)
        self.ask(KEEP)
        self.remove(LOVE)
        self.assertEqual(self.rows(), [KEEP])

    def test_underscores_and_case_still_match(self):
        self.ask(PASTED)
        self.ask(KEEP)
        self.remove("some_artist_-_13_-_LOVE.FLAC  ::INFO:: 36.82MB")
        self.assertEqual(self.rows(), [KEEP])

    def test_a_row_being_sent_stays_and_the_notice_names_the_bare_file(self):
        self.ask(PASTED)
        self.ask(KEEP)
        row = config.dcc_queue[USER][0]
        config.active_transfers.append({"user": USER, "file": LOVE, "bytes_sent": 0, "queue_row": row})

        printed = self.remove(PASTED)

        self.assertEqual(self.rows(), [LOVE, KEEP])
        told = self.notices()
        self.assertEqual(len(told), 1, told)
        self.assertIn('"%s" is being sent to you right now, so it cannot be removed.' % LOVE, told[0])
        self.assertNotIn("::INFO::", told[0])
        self.assertIn("removed 0 queued file(s) matching %r" % LOVE, printed)

    def test_a_pasted_line_for_a_file_not_queued_says_so_with_the_bare_name(self):
        self.ask(KEEP)

        printed = self.remove(PASTED)

        self.assertEqual(self.rows(), [KEEP])
        told = self.notices()
        self.assertIn('"%s" is not in your queue.' % LOVE, told[0])
        self.assertNotIn("::INFO::", told[0])
        self.assertIn("removed 0 queued file(s) matching %r" % LOVE, printed)


class TheRoutesIn(Library):
    """The two ways a user types it: `@<nick>-remove <line>` and the CTCP
    `REMOVE <line>`, each through irc.py to the real handler."""

    def setUp(self):
        super().setUp()
        stub = types.ModuleType("threading")
        stub.__dict__.update(threading.__dict__)
        stub.Thread = InlineIrcThread
        real = irc.threading
        irc.threading = stub
        self.addCleanup(setattr, irc, "threading", real)
        self.ask(PASTED)
        self.ask(KEEP)

    def test_the_at_nick_remove_command(self):
        msg = "@%s-remove %s" % (NICK, PASTED)
        with contextlib.redirect_stdout(io.StringIO()):
            irc.answer_command(msg, msg.lower(), USER, "u@host.example", OTHER,
                               irc.get_bot_aliases(), self.sock, self.oserve)

        self.assertEqual(self.rows(), [KEEP])
        self.assertIn('Removed "%s" from your queue.' % LOVE, self.notices()[-1])

    def test_the_ctcp_remove(self):
        msg = "\x01REMOVE %s\x01" % PASTED
        line = ":%s!u@host.example PRIVMSG %s :%s" % (USER, OTHER, msg)
        with contextlib.redirect_stdout(io.StringIO()):
            irc.answer_ctcp(line, msg, USER, OTHER, self.sock, self.oserve)

        self.assertEqual(self.rows(), [KEEP])
        self.assertIn('Removed "%s" from your queue.' % LOVE, self.notices()[-1])


class QueuedNameOfRequest(unittest.TestCase):

    def test_the_tail_comes_off(self):
        self.assertEqual(dcc.queued_name_of_request(PASTED), LOVE)
        self.assertEqual(dcc.queued_name_of_request(LOVE + "  ::info:: 1MB"), LOVE)
        self.assertEqual(dcc.queued_name_of_request("name.mp3 ---- 18.8Mb"), "name.mp3")

    def test_a_bare_name_is_unchanged(self):
        for name in (LOVE, "Some_Artist_-_13_-_Love.flac", "a.mp3", "  spaced.flac  "):
            with self.subTest(name=name):
                self.assertEqual(dcc.queued_name_of_request(name), name.strip())

    def test_the_folder_in_front_goes(self):
        self.assertEqual(dcc.queued_name_of_request("Album/track.flac"), "track.flac")
        self.assertEqual(dcc.queued_name_of_request("/Album/track.flac"), "track.flac")
        self.assertEqual(dcc.queued_name_of_request("Album/track.flac  ::INFO:: 4KB"), "track.flac")

    def test_a_folder_alone_names_no_file(self):
        for text in ("/", "Album/", "Album/  ::INFO:: 4KB"):
            with self.subTest(text=text):
                self.assertEqual(dcc.queued_name_of_request(text), "")

    def test_a_backslash_stays_part_of_the_name_off_windows(self):
        if os.path is ntpath:
            self.skipTest("Windows: a backslash separates folders there")
        self.assertEqual(dcc.queued_name_of_request("Album\\track.flac"), "Album\\track.flac")

    def test_a_backslash_separates_on_windows(self):
        if os.path is not ntpath:
            self.skipTest("not Windows: a backslash is part of a name here")
        self.assertEqual(dcc.queued_name_of_request("Album\\track.flac"), "track.flac")

    def test_it_is_what_the_request_s_own_split_gives(self):
        """The request and the remove share one strip: the name half of
        _requested_name_and_size(), and its size hint is lower case."""
        name, hint = dcc._requested_name_and_size(PASTED)
        self.assertEqual(name, LOVE)
        self.assertEqual(hint, "36.82mb 3m1s 1681/44.1/s")
        self.assertEqual(dcc._requested_name_and_size(LOVE), (LOVE, ""))


if __name__ == "__main__":
    unittest.main()
