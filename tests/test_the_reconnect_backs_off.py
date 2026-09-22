"""A flat ten-second reconnect could sit inside the server's throttle window
for ever, and the throttle's own ERROR line was read and dropped (audit
M61, #663).

Every reconnect path slept exactly 10 s. ircu's IPcheck throttles an
address that reconnects too often inside its clone period (4 in 40 s by
default) - counting refused connects too, and resetting only after a gap
longer than the period - so once a run of drops tripped it, the 10 s
cadence kept it tripped: each attempt was answered with `ERROR :Your host
is trying to (re)connect too fast -- throttled` and closed, and the bot
never got back on by itself. The ERROR line itself went unprinted, so the
log said "Server closed connection" and nothing about why.

The wait doubles from 10 s to a five-minute ceiling for attempts that never
reach a 001, and a connection that registers resets it - an ordinary drop
still comes back in ten seconds. The ERROR line is printed, and a throttle
is named for what it is. Driven for real: irc.irc_loop() against a series
of scripted connections, the sleep between them recorded.
"""

import contextlib
import io
import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402

from tests import test_a_third_nick_when_both_are_taken as ladder  # noqa: E402
from tests import test_the_bots_own_nick_follows_the_server as own  # noqa: E402

THROTTLED = "ERROR :Your host is trying to (re)connect too fast -- throttled"
WELCOME = ":irc.example.net 001 SomeBot :Welcome to the network, SomeBot"


class TheCurve(unittest.TestCase):

    def test_doubles_from_ten_seconds_to_five_minutes(self):
        self.assertEqual([irc.reconnect_delay(n) for n in range(0, 8)],
                         [10.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0])

    def test_the_first_wait_is_what_it_always_was(self):
        self.assertEqual(irc.RECONNECT_DELAY_FIRST, 10.0)
        self.assertEqual(irc.reconnect_delay(1), 10.0)


class ASeriesOfConnections(own.DrivesPastRegistration):
    """The ladder harness with the post-001 threads stubbed (a connection
    that registers would otherwise start the JOIN timer, whose own sleep
    this records), extended: one scripted socket per connection attempt,
    handed out in order, and every reconnect sleep recorded. The loop is
    stopped by the sleep after the last scripted connection."""

    def run_connections(self, *connections):
        sockets = [ladder._ScriptedSocket([(l + "\r\n").encode("utf-8") for l in lines])
                   for lines in connections]
        handed = iter(sockets)
        irc.socket.socket = lambda *a, **k: next(handed)
        waits = []

        def sleep_records_then_stops(seconds):
            waits.append(seconds)
            if len(waits) == len(connections):
                raise ladder._StopTheLoop()
        irc.time.sleep = sleep_records_then_stops
        output = io.StringIO()
        outcome = {}

        def run():
            with contextlib.redirect_stdout(output):
                try:
                    irc.irc_loop()
                except ladder._StopTheLoop:
                    outcome["stopped"] = True
                except BaseException as err:  # noqa: BLE001
                    outcome["error"] = repr(err)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(15)
        self.assertFalse(thread.is_alive(), "irc_loop() did not get through the scripted connections")
        self.assertEqual(outcome, {"stopped": True})
        return waits, output.getvalue()

    def test_throttled_attempts_wait_longer_each_time(self):
        """The audit's probe: a fake ircu that answers every NICK with the
        throttle ERROR and closes. It measured 10 s, 10 s, 10 s..."""
        waits, _out = self.run_connections([THROTTLED], [THROTTLED], [THROTTLED], [THROTTLED])

        self.assertEqual(waits, [10.0, 20.0, 40.0, 80.0])

    def test_a_connection_that_registers_resets_the_wait(self):
        waits, _out = self.run_connections([THROTTLED], [THROTTLED], [WELCOME], [THROTTLED])

        self.assertEqual(waits, [10.0, 20.0, 10.0, 10.0])

    def test_an_ordinary_drop_after_registration_comes_back_in_ten_seconds(self):
        waits, _out = self.run_connections([WELCOME], [WELCOME])

        self.assertEqual(waits, [10.0, 10.0])

    def test_the_servers_last_word_is_printed_and_the_throttle_named(self):
        _waits, out = self.run_connections([THROTTLED])

        self.assertIn("[SERVER] " + THROTTLED, out)
        self.assertIn("comes back too fast", out)
        self.assertIn("Reconnecting to the IRC server in 10 seconds", out)

    def test_the_wait_is_said_out_loud_as_it_grows(self):
        _waits, out = self.run_connections([THROTTLED], [THROTTLED], [THROTTLED])

        self.assertIn("Reconnecting to the IRC server in 20 seconds", out)
        self.assertIn("Reconnecting to the IRC server in 40 seconds", out)


for _name in [n for n in dir(own.DrivesPastRegistration) if n.startswith("test")]:
    setattr(ASeriesOfConnections, _name, None)


if __name__ == "__main__":
    unittest.main()
