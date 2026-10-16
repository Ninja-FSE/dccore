"""#1306: the welcome, the JOIN, the watchdog and the activation, each with
the connection it is for - step 3 of splitting irc_loop().

They were closures over irc_loop()'s frame: answer_welcome() was the 001/376
branch, and delayed_join(), activation_watchdog() and delayed_activate() read
the loop's `joined`, `channels_confirmed` and `s` whenever they woke. Each
now takes an irc.Connection, so they are called directly here - with a
recording socket, no sleeping, and every thread they start recorded rather
than run.

WelcomeHarness is shared with the tests that read these functions' text
until #1306 (test_a_join_nobody_checked.py, test_channel_list.py,
test_on_connect_commands_are_checked.py, test_the_servers_shortened_nick_
is_adopted.py).

Every nick, channel and server here is invented.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import on_connect  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402
from tests.test_the_bots_own_nick_follows_the_server import (  # noqa: E402
    NOTICE_AUTH, DrivesPastRegistration, welcome)


class RecordingSocket:
    def __init__(self, timeline=None):
        self.sent = []
        self.timeline = timeline if timeline is not None else []

    def sendall(self, payload):
        line = payload.decode("utf-8", "replace").strip()
        self.sent.append(line)
        self.timeline.append(("sent", line))


class RecordedThread:
    """Stands in for threading.Thread in irc: remembers what would start."""

    started = None
    timeline = None

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, tuple(args)

    def start(self):
        RecordedThread.started.append((self.target, self.args))
        RecordedThread.timeline.append(("started", self.target))


class WelcomeHarness(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="alfa", ORIGINAL_NICK="alfa", CHANNEL="#somechannel",
                        DEBUG_CHANNEL="", MY_IP_OR_DOCK="203.0.113.5")
        # One timeline across the socket, the threads and the resend mark,
        # for the tests that are about order.
        self.timeline = []
        self.sock = RecordingSocket(self.timeline)
        # Through set_config() and patch(), so what a test moves comes back.
        self.set_config(connection_epoch=getattr(config, "connection_epoch", 0) + 1,
                        activation_triggered=False)
        self.patch(announce, "is_ready", getattr(announce, "is_ready", False))
        self.conn = irc.Connection(self.sock, config.connection_epoch)
        RecordedThread.started = []
        RecordedThread.timeline = self.timeline
        self.patch(irc.threading, "Thread", RecordedThread)
        self.slept = []
        self.patch(irc.time, "sleep", self.slept.append)
        self.marked = []
        self.patch(on_connect, "mark_sent",
                   lambda epoch: (self.marked.append(epoch), self.timeline.append(("marked", epoch))))
        self.patch(on_connect, "load", lambda: ([], 0.0))
        self.debug = silence_debug(announce)

    def patch(self, owner, name, value):
        real = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, real)

    def started(self, target):
        return [args for started_target, args in RecordedThread.started if started_target is target]

    def quietly(self, call, *args):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            result = call(*args)
        return result, said.getvalue()


class TheConnection(unittest.TestCase):

    def test_it_starts_unregistered_with_nothing_confirmed(self):
        sock = object()
        conn = irc.Connection(sock, 7)
        self.assertEqual((conn.sock, conn.epoch, conn.joined, conn.channels_confirmed), (sock, 7, False, set()))

    def test_each_has_its_own_set(self):
        self.assertIsNot(irc.Connection(None, 1).channels_confirmed, irc.Connection(None, 2).channels_confirmed)


class TheWelcome(WelcomeHarness):

    def test_it_registers_and_starts_the_join_for_this_connection(self):
        self.quietly(irc.answer_welcome, ":irc.example.net 001 alfa :Welcome", self.conn)

        self.assertTrue(self.conn.joined)
        self.assertEqual(self.started(irc.delayed_join), [(self.conn, irc.channels_we_should_be_in())])


class TheJoin(WelcomeHarness):

    def test_it_joins_on_its_own_socket_and_starts_its_own_watchdog(self):
        self.quietly(irc.delayed_join, self.conn, ["#somechannel"])

        self.assertEqual(self.sock.sent, ["JOIN #somechannel"])
        self.assertEqual(self.started(irc.activation_watchdog), [(self.conn,)])
        self.assertEqual(self.started(on_connect.watch), [(self.sock, self.conn.epoch)])
        self.assertEqual(self.marked, [self.conn.epoch])


class TheJoinAfterAReconnect(WelcomeHarness):

    def test_a_reconnect_in_its_sleep_does_not_hand_it_the_new_epoch(self):
        """The reason the epoch was bound as a default argument (#1066): read
        when it wakes, it would be the next connection's."""
        config.connection_epoch += 1

        self.quietly(irc.delayed_join, self.conn, ["#somechannel"])

        self.assertEqual(self.marked, [self.conn.epoch])
        self.assertEqual(self.started(on_connect.watch), [(self.sock, self.conn.epoch)])
        self.assertNotEqual(self.conn.epoch, config.connection_epoch)


class TheWatchdog(WelcomeHarness):

    def test_a_watchdog_from_a_dead_connection_stands_down(self):
        self.conn.joined = True
        config.activation_triggered = False
        config.connection_epoch += 1

        _result, said = self.quietly(irc.activation_watchdog, self.conn)

        self.assertIn("Standing down", said)
        self.assertFalse(config.activation_triggered)
        self.assertEqual(self.started(irc.delayed_activate), [])

    def test_an_unregistered_connection_is_not_activated(self):
        config.activation_triggered = False
        self.quietly(irc.activation_watchdog, self.conn)
        self.assertEqual(self.started(irc.delayed_activate), [])

    def test_everything_confirmed_activates_quietly(self):
        self.conn.joined = True
        self.conn.channels_confirmed.add("#somechannel")
        config.activation_triggered = False

        _result, said = self.quietly(irc.activation_watchdog, self.conn)

        self.assertIn("every channel confirmed just in time", said)
        self.assertEqual(self.started(irc.delayed_activate), [(self.conn,)])
        self.assertEqual(self.slept, [irc.ACTIVATION_TIMEOUT])


class TheActivation(WelcomeHarness):

    def test_the_nick_monitor_watches_this_activations_socket(self):
        """It was started with irc_loop's current `s`, read through the frame."""
        self.patch(irc, "claim_channel_sync", lambda sock, epoch, **kw: True)
        self.patch(announce, "announce_worker", lambda: None)

        self.quietly(irc.delayed_activate, self.conn)

        monitors = [args for target, args in RecordedThread.started
                    if getattr(target, "__name__", "") == "background_nick_monitor"]
        self.assertEqual(monitors, [(self.sock,)])
        self.assertTrue(announce.is_ready)

    def test_a_dead_connection_is_not_activated(self):
        announce.is_ready = False
        config.connection_epoch += 1
        self.quietly(irc.delayed_activate, self.conn)
        self.assertFalse(announce.is_ready)
        self.assertEqual(RecordedThread.started, [])


class The366HandlerInTheReadLoop(DrivesPastRegistration):
    """The other way in to delayed_activate(): the last 366 the bot waited
    for, in the real read loop against a scripted server."""

    def test_the_last_end_of_names_activates_this_connection(self):
        started = []

        class Recorded:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
                self.target, self.args = target, tuple(args)

            def start(self):
                started.append((self.target, self.args))

        irc.threading.Thread = Recorded   # the harness's own stub module, put back by it
        self.set_config(activation_triggered=False)
        self.run_registration(NOTICE_AUTH, welcome("SomeBot"),
                              ":irc.example.net 366 SomeBot #somechannel :End of /NAMES list.")

        activated = [args for target, args in started if target is irc.delayed_activate]
        self.assertEqual(len(activated), 1, started)
        conn = activated[0][0]
        self.assertIsInstance(conn, irc.Connection)
        self.assertEqual(conn.channels_confirmed, {"#somechannel"})
        self.assertTrue(conn.joined)


class TheBackoffAsksTheConnection(DrivesPastRegistration):
    """#663: the wait before the next attempt grows only while links fail to
    register. Whether this one did is its conn.joined now (#1306)."""

    def asked(self, *lines):
        counts = []
        real = irc.reconnect_delay
        irc.reconnect_delay = lambda failures: (counts.append(failures), real(failures))[1]
        self.addCleanup(setattr, irc, "reconnect_delay", real)
        self.run_registration(*lines)
        return counts

    def test_a_link_that_registered_starts_the_count_again(self):
        self.assertEqual(self.asked(NOTICE_AUTH, welcome("SomeBot")), [0])

    def test_a_link_that_never_registered_counts(self):
        self.assertEqual(self.asked(NOTICE_AUTH), [1])


if __name__ == "__main__":
    unittest.main()
