"""A handful of CTCPs could take every DCC port for a minute.

WHAT WENT WRONG

handle_dcc_chat()'s passive branch spawns _listen_and_serve() immediately.
Every other limit on that path runs AFTER accept():

  * is_bad_ip() is consulted on the connecting address, which does not exist
    until somebody connects;
  * the single-_pending rule inside _serve() applies to a session that already
    exists;
  * and irc.py deliberately leaves DCC CHAT out of the set
    security.is_flooding() meters, so the CTCPs that start them are not
    rate-limited either.

Nothing bounded how many listeners could be OPEN at once. So a handful of
passive DCC CHAT offers took every port in DCC_PORT_START..DCC_PORT_END and
held them for LISTEN_TIMEOUT - the same range DCC SEND needs. The bot stops
being able to send files at all, and recovers only when the listeners time
out.

Found by the full-program audit.

THE FIX

One listener at a time, which is the same shape as the _pending rule one step
further on - "at most one connected-but-unauthenticated session" - applied to
the step before it. A refused offer costs the sender nothing but another CTCP
once the current one resolves, and a real operator makes one at a time.
"""

import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import adminchat  # noqa: E402


class OnlyOneListenerAtATime(unittest.TestCase):

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        self.original = adminchat._listen_and_serve_locked
        self.addCleanup(setattr, adminchat, "_listen_and_serve_locked",
                        self.original)

    def test_a_second_offer_while_one_is_waiting_is_refused(self):
        """The whole defect: nothing stopped the second, third and tenth."""
        started = threading.Event()
        release = threading.Event()
        entered = []

        def blocking(_sock, nick, _host, _token=None):
            entered.append(nick)
            started.set()
            release.wait(5)

        adminchat._listen_and_serve_locked = blocking

        first = threading.Thread(
            target=adminchat._listen_and_serve,
            args=(None, "first", "host", None), daemon=True)
        first.start()
        self.assertTrue(started.wait(5))

        # Straight through, on this thread: it must return without listening.
        adminchat._listen_and_serve(None, "second", "host", None)

        self.assertEqual(entered, ["first"])
        release.set()
        first.join(5)

    def test_ten_concurrent_offers_open_one_listener(self):
        """The reported shape - a handful of CTCPs at once."""
        release = threading.Event()
        entered = []
        lock = threading.Lock()

        def blocking(_sock, nick, _host, _token=None):
            with lock:
                entered.append(nick)
            release.wait(5)

        adminchat._listen_and_serve_locked = blocking

        threads = [threading.Thread(target=adminchat._listen_and_serve,
                                    args=(None, f"nick{i}", "host", None),
                                    daemon=True)
                   for i in range(10)]
        for thread in threads:
            thread.start()

        # WAIT FOR THE LOSERS TO HAVE LOST, rather than sleeping and hoping.
        #
        # `_listening` is cleared in _listen_and_serve()'s `finally`, so the
        # moment the winner returns the gate is open again. A refused thread
        # exits immediately, so "nine of them have finished" is exactly the
        # condition that every loser has already been turned away - and until
        # that holds, releasing the winner lets a straggler through and the
        # count is 2 through no fault of the code under test.
        #
        # The old 0.2s sleep assumed ten threads always get scheduled inside
        # 200ms. They do on an idle machine; they did not on a CI runner that
        # was simultaneously reporting "[Errno 24] Too many open files", where
        # this failed on ubuntu/3.10 alone while the other five legs passed.
        deadline = time.time() + 5
        while time.time() < deadline:
            if sum(1 for thread in threads if thread.is_alive()) <= 1:
                break
            time.sleep(0.01)

        # The wait must have SUCCEEDED, not timed out - otherwise a machine
        # slow enough to blow the deadline would reach the assertion below
        # with the losers still pending and pass it for the wrong reason,
        # which is the failure this test exists to catch.
        still_running = sum(1 for thread in threads if thread.is_alive())
        self.assertEqual(still_running, 1,
                         f"{still_running} threads were still running after "
                         f"5s; the other nine should have been refused "
                         f"immediately")

        release.set()
        for thread in threads:
            thread.join(5)

        self.assertEqual(len(entered), 1,
                         f"{len(entered)} listeners were opened at once")

    def test_the_next_offer_works_once_the_first_finishes(self):
        """Refusing must not be sticky - the console has to stay usable."""
        entered = []
        adminchat._listen_and_serve_locked = (
            lambda _s, nick, _h, _t=None: entered.append(nick))

        adminchat._listen_and_serve(None, "first", "host", None)
        adminchat._listen_and_serve(None, "second", "host", None)

        self.assertEqual(entered, ["first", "second"])

    def test_the_flag_is_cleared_when_the_listener_raises(self):
        """Otherwise one failed offer wedges the console for good, which is
        worse than the exhaustion this exists to prevent."""
        def boom(_sock, _nick, _host, _token=None):
            raise RuntimeError("bang")

        adminchat._listen_and_serve_locked = boom

        with self.assertRaises(RuntimeError):
            adminchat._listen_and_serve(None, "first", "host", None)

        self.assertFalse(adminchat._listening)

    def test_reset_for_tests_clears_it(self):
        """It is module state like _session and _pending, and a test whose
        listener thread outlives it would otherwise refuse every passive offer
        in every test that ran afterwards - which is exactly what happened
        before this was added."""
        adminchat._listening = True

        adminchat.reset_state_for_tests()

        self.assertFalse(adminchat._listening)


if __name__ == "__main__":
    unittest.main()
