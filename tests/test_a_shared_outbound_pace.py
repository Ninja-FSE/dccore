"""One clock for every outbound send, not two that never met.

Reported live (#406): an operator was killed with Excess Flood shortly after
growing their bot to 14 channels. queue_mgr.py's queue_worker slept MSG_DELAY
after every send; announce.py's debug drain slept DEBUG_MSG_DELAY after every
send, on its own thread, unaware the first thread existed. The server only
ever sees the SUM of what both lanes send - on stock settings, up to 2.2
lines/second against a server that kills a client sustaining much more than
about 0.5 - and nothing an operator can see names that sum.

runtime.OutboundPacer replaces both independent timers with one shared clock:
whichever lane sends next, from either thread, reserves the clock for its own
interval before anyone else's next send. The combined rate can therefore
never exceed one interval's worth of traffic, however the two lanes
interleave.
"""

import contextlib
import io
import os
import threading
import time
import unittest

from tests.support import DCCoreTestCase

import announce
import commands
import defaults as config
import queue_mgr
import runtime


class OutboundPacerTests(unittest.TestCase):
    """The clock itself, with no daemon code involved."""

    def setUp(self):
        self.pacer = runtime.OutboundPacer()

    def test_the_first_call_does_not_wait(self):
        started = time.monotonic()
        self.pacer.wait_for_slot(0.2)
        self.assertLess(time.monotonic() - started, 0.05,
                        "an unclaimed pacer must not delay the first sender")

    def test_a_second_call_waits_out_the_interval(self):
        self.pacer.wait_for_slot(0.15)
        started = time.monotonic()
        self.pacer.wait_for_slot(0.15)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.13,
                                "the second reservation must wait for the first's slot")

    def test_a_slot_is_reserved_even_if_the_caller_never_sends(self):
        """Both queue_mgr.py and announce.py call wait_for_slot() BEFORE
        attempting the socket write, so a failed send still costs its slot
        rather than letting a broken pipe retry in a tight loop. Modelled
        here simply as: nothing about the call depends on what happens after
        it returns."""
        self.pacer.wait_for_slot(0.1)
        started = time.monotonic()
        self.pacer.wait_for_slot(0.1)
        self.assertGreaterEqual(time.monotonic() - started, 0.08)

    def test_two_threads_are_serialised_onto_the_same_clock(self):
        """The property the whole fix rests on: it does not matter which
        thread asks, the clock is shared - this is what makes two lanes on
        two threads behave as one budget instead of two that sum."""
        interval = 0.05
        calls_per_thread = 5
        timestamps = []
        lock = threading.Lock()

        def hammer():
            for _ in range(calls_per_thread):
                self.pacer.wait_for_slot(interval)
                with lock:
                    timestamps.append(time.monotonic())

        threads = [threading.Thread(target=hammer) for _ in range(2)]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        total_slots = calls_per_thread * len(threads)
        self.assertEqual(len(timestamps), total_slots)
        elapsed = time.monotonic() - started
        # If the two threads paced independently instead of sharing the
        # clock - the bug this fixes - each would only wait out its OWN
        # history and the whole thing would finish in about half this time.
        self.assertGreaterEqual(elapsed, (total_slots - 1) * interval * 0.8)


class _SleepShim:
    """Replacement for a module's `time`, so a worker loop spins fast in the
    test and can be told to stop.

    Caps every sleep and raises SystemExit once stopped. SystemExit is not
    an Exception, so it escapes both queue_worker's and the debug drain's
    catch-all handlers and ends the thread - neither loop has any other way
    to stop, by design, in production.

    Does NOT intercept runtime.OutboundPacer.wait_for_slot(): that sleeps on
    its own `time` import in runtime.py, not on the caller's. Kept genuinely
    short in these tests (MSG_DELAY/DEBUG_MSG_DELAY in the tens of
    milliseconds) so the gap between a stop request and the thread actually
    exiting stays well inside the join() timeout below.
    """

    def __init__(self, cap=0.02):
        self.cap = cap
        self.stopped = threading.Event()

    def sleep(self, seconds):
        if self.stopped.is_set():
            raise SystemExit
        time.sleep(min(float(seconds), self.cap))
        if self.stopped.is_set():
            raise SystemExit

    def time(self):
        return time.time()


class TimestampedSocket:
    """Stands in for the live IRC socket and remembers WHEN each write
    landed, not just what - the property under test is timing, not content.
    """

    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append((time.monotonic(), payload))
        return len(payload)

    def sendall(self, payload):
        self.sent.append((time.monotonic(), payload))

    def close(self):
        pass


class TheCombinedOutboundRateIsCapped(DCCoreTestCase):
    """End to end: queue_mgr's worker and announce's debug drain, running as
    the real threads they are in production, sharing one socket and one
    runtime.outbound_pacer."""

    def setUp(self):
        super().setUp()
        # Through set_config() (#667): set directly, the tiny pace outlived
        # the test and every later one ran at it.
        self.set_config(MSG_DELAY=0.05,
                        DEBUG_MSG_DELAY=0.01)  # deliberately far below MSG_DELAY
        config.vip_queue = []
        config.send_queue = {}
        config.bot_joined_channel = True
        self.oserve.bot_joined_channel = True
        # The pump also waits for activation (#630); the harness resets this.
        config.activation_triggered = True

        # A fresh clock per test - the real one is a process-wide singleton
        # and other tests must not inherit the moment this one's last line
        # left (the value itself is restored by set_config above).
        runtime.outbound_pacer = runtime.OutboundPacer()

        self.sock = TimestampedSocket()
        self.oserve.irc_connection = self.sock

        self._real_queue_time = queue_mgr.time
        self._real_announce_time = announce.time
        self.queue_shim = _SleepShim()
        self.debug_shim = _SleepShim()
        queue_mgr.time = self.queue_shim
        announce.time = self.debug_shim

        self.queue_worker_thread = None
        self.debug_drain_thread = None

    def tearDown(self):
        self.queue_shim.stopped.set()
        self.debug_shim.stopped.set()
        if self.queue_worker_thread is not None:
            self.queue_worker_thread.join(timeout=3.0)
        if self.debug_drain_thread is not None:
            self.debug_drain_thread.join(timeout=3.0)
        queue_mgr.time = self._real_queue_time
        announce.time = self._real_announce_time
        runtime.outbound_pacer = runtime.OutboundPacer()
        super().tearDown()

    def start_queue_worker(self):
        def run():
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    queue_mgr.queue_worker()
                except SystemExit:
                    pass
        self.queue_worker_thread = threading.Thread(target=run, daemon=True)
        self.queue_worker_thread.start()

    def start_debug_drain(self):
        # Calling _debug_drain_worker() directly, rather than going through
        # send_debug()/_ensure_debug_drain(), sidesteps that pair's own
        # process-wide "already started?" latch entirely - this test wants
        # its OWN drain, on its OWN shimmed clock, not whichever one an
        # earlier test in the suite may have left running.
        announce._debug_drain_id = "test-drain"

        def run():
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    announce._debug_drain_worker("test-drain")
                except SystemExit:
                    pass
        self.debug_drain_thread = threading.Thread(target=run, daemon=True)
        self.debug_drain_thread.start()

    def test_the_combined_rate_never_exceeds_one_shared_slot_per_interval(self):
        for i in range(4):
            config.send_queue.setdefault("dave", []).append(
                f"PRIVMSG dave :queue line {i}\r\n")
        for i in range(4):
            announce._debug_queue.append(f"PRIVMSG #debug :debug line {i}\r\n")

        started = time.monotonic()
        self.start_queue_worker()
        self.start_debug_drain()

        deadline = time.time() + 3.0
        while time.time() < deadline and len(self.sock.sent) < 8:
            time.sleep(0.02)

        self.assertEqual(len(self.sock.sent), 8,
                         "all 4 queue lines and 4 debug lines must eventually be delivered")

        elapsed = self.sock.sent[-1][0] - started
        # 8 sends sharing ONE clock need at least 7 slots at the larger of
        # the two configured delays - here that resolves to MSG_DELAY for
        # both lanes, about 0.35s total. Two INDEPENDENT clocks - the bug
        # this fixes - would let the fast debug lane and the slower queue
        # lane run in parallel threads and finish in about 0.2s: whichever
        # lane is slower on its own, not the sum of both lanes' reservations.
        self.assertGreaterEqual(
            elapsed, 7 * config.MSG_DELAY * 0.8,
            "the two lanes must share one clock, not pace independently")


class TheStandardLaneIsNoLongerStarvedByVip(DCCoreTestCase):
    """#426: the VIP lane used to `continue` straight back to the top after
    every send, so the standard lane below never ran at all for as long as
    ANYTHING remained in vip_queue - reachable by a single nick well inside
    the ordinary flood limits (is_flooding() permits 10 commands/5s; -help
    alone used to queue 5 VIP lines per request). Runs the real queue_worker
    thread, same fixture shape as TheCombinedOutboundRateIsCapped above.
    """

    def setUp(self):
        super().setUp()
        self.set_config(MSG_DELAY=0.01)
        config.vip_queue = []
        config.send_queue = {}
        config.bot_joined_channel = True
        self.oserve.bot_joined_channel = True
        # The pump also waits for activation (#630); the harness resets this.
        config.activation_triggered = True
        runtime.outbound_pacer = runtime.OutboundPacer()
        self.sock = TimestampedSocket()
        self.oserve.irc_connection = self.sock
        self._real_queue_time = queue_mgr.time
        self.queue_shim = _SleepShim()
        queue_mgr.time = self.queue_shim
        self.queue_worker_thread = None

    def tearDown(self):
        self.queue_shim.stopped.set()
        if self.queue_worker_thread is not None:
            self.queue_worker_thread.join(timeout=3.0)
        queue_mgr.time = self._real_queue_time
        runtime.outbound_pacer = runtime.OutboundPacer()
        super().tearDown()

    def start_queue_worker(self):
        def run():
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    queue_mgr.queue_worker()
                except SystemExit:
                    pass
        self.queue_worker_thread = threading.Thread(target=run, daemon=True)
        self.queue_worker_thread.start()

    def sent_bytes(self):
        return [payload for _t, payload in self.sock.sent]

    def test_a_large_vip_backlog_does_not_delay_an_ordinary_reply(self):
        """A backlog too large to drain within the test's own deadline at
        MSG_DELAY's pace (2000 lines * 0.01s = 20s) - so seeing dave's reply
        arrive well inside that deadline is only possible if the standard
        lane runs WHILE vip_queue is still non-empty, not after it empties."""
        for i in range(2000):
            config.vip_queue.append(f"PRIVMSG #chan :vip {i}\r\n")
        config.send_queue["dave"] = ["NOTICE dave :your reply\r\n"]

        self.start_queue_worker()

        deadline = time.time() + 1.5
        delivered = False
        while time.time() < deadline:
            if any(b"NOTICE dave" in payload for payload in self.sent_bytes()):
                delivered = True
                break
            time.sleep(0.01)

        self.assertTrue(delivered,
                        "dave's own reply never went out while VIP had a "
                        "large backlog - the standard lane was starved")

    def test_vip_still_drains_promptly_when_it_has_a_backlog(self):
        """Control: the fix must not have slowed VIP down to achieve
        fairness - it should still send on every iteration it has
        something, exactly as before."""
        for i in range(20):
            config.vip_queue.append(f"PRIVMSG #chan :vip {i}\r\n")

        self.start_queue_worker()

        # A CEILING, not a delay: the loop returns the moment the twentieth
        # line lands, which on a quiet machine is well under a second. It
        # was 2.0 s, and macOS's hosted runners delivered 17 and 19 of 20 in
        # that - the same scheduler jitter #548 met - so the test failed on
        # timing while the property it asserts held. Ten seconds costs a
        # healthy run nothing and a loaded one the slack it needs.
        deadline = time.time() + 10.0
        while time.time() < deadline and len(self.sock.sent) < 20:
            time.sleep(0.01)

        self.assertEqual(len(self.sock.sent), 20,
                         "VIP no longer drains at its own pace")


class TheThirdUnpacedWriterIsFixedToo(unittest.TestCase):
    """A THIRD path fed the server unpaced traffic, found reviewing this
    fix: irc.py's CTCP VERSION reply and its "!debugnames" RAM-CHECK notice
    both wrote straight to the socket from inside irc_loop(), sharing
    nothing with queue_mgr.py's or announce.py's now-shared clock.

    Fits the reported trigger (#406) at least as well as the reconnect
    burst: many ordinary IRC clients send exactly one CTCP VERSION, unasked,
    the moment they see a new nick - a bot joining 14 channels at once can
    collect a dozen of those within a second or two. Ten different users
    asking once each is ten different requests, so the per-user flood gate
    (is_bot_command's own throttle) never sees a repeat offender to catch -
    the gate answers "is THIS user flooding", not "is the SOCKET flooding".

    Structural, not executed: irc_loop() is the single monolithic function
    reading a live socket that this suite does not run line by line (same
    caveat as ReconnectThawSummaryTests' own wiring check in
    tests/test_reconnect.py).
    """

    def source(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "irc.py"), encoding="utf-8") as f:
            return f.read()

    def version_reply_block(self):
        return self.source().split('if ctcp_cmd == "VERSION":', 1)[1].split(
            "continue", 1)[0]

    def debugnames_block(self):
        # Up to the next branch of the same chain, not a fixed number of
        # characters: the admin gate in front of the notice pushed the
        # queue_message() line past the 1500 this used to take.
        rest = self.source().split('elif msg.lower() == "!debugnames":', 1)[1]
        return rest[:rest.index('elif msg.lower() == "!ping":')]

    def test_the_version_reply_no_longer_writes_the_socket_directly(self):
        self.assertNotIn("s.send(version_reply", self.version_reply_block())

    def test_the_version_reply_is_queued_vip(self):
        block = self.version_reply_block()
        self.assertIn("oserve.queue_message(user, version_reply, is_vip=True)",
                      block)

    def test_the_ram_check_notice_no_longer_writes_the_socket_directly(self):
        block = self.debugnames_block()
        self.assertNotIn("s.send(", block)

    def test_the_ram_check_notice_is_queued_vip(self):
        block = self.debugnames_block()
        self.assertIn("oserve.queue_message(user, ram_check, is_vip=True)", block)

    def test_the_vip_lane_is_itself_paced(self):
        """The point of routing through queue_message(is_vip=True) rather
        than some other fix: the VIP lane already reserves a slot from
        runtime.outbound_pacer before every send (queue_mgr.py), so these
        two gain that pacing for free instead of a third bespoke mechanism."""
        vip_lane = queue_mgr_source().split(
            'if hasattr(config, \'vip_queue\') and config.vip_queue:', 1)[1][:700]
        self.assertIn("runtime.outbound_pacer.wait_for_slot(config.MSG_DELAY)",
                      vip_lane)


class TheFourthUnpacedWriterIsFixedToo(DCCoreTestCase):
    """#425: commands.handle_ping_request() wrote straight to the socket too
    - the ONE responder any nick can trigger without being an admin, so
    several clone connections calling !ping aggregate into unthrottled
    traffic the same way the VERSION/RAM-CHECK writers above did.

    Behavioural, unlike the structural checks above: unlike the CTCP VERSION
    reply and the RAM-CHECK notice, which live inline inside irc_loop() and
    are not otherwise reachable, handle_ping_request() is a real, directly
    callable function - so this drives it for real rather than reading its
    source.

    NOT routed through oserve.queue_message(is_vip=True) like the other two:
    !ping measures its own round-trip latency from config.ping_start_time,
    and queueing the PING behind the VIP lane would fold however long it
    waited there into the number the command exists to report. A direct
    wait_for_slot() puts the send on the same shared clock without that
    side effect - see handle_ping_request()'s own docstring.
    """

    def setUp(self):
        super().setUp()
        self.real_pacer = runtime.outbound_pacer
        runtime.outbound_pacer = runtime.OutboundPacer()
        self.addCleanup(setattr, runtime, "outbound_pacer", self.real_pacer)
        # The pingers below are admins: !ping answers only the bot's own
        # admin now (tests/test_diagnostics_answer_only_the_admin.py), and
        # what this class measures is the pacing of a ping that IS sent.
        self.set_config(MSG_DELAY=0.1, ADMIN_NICK="alice, bob")

    def test_a_second_ping_waits_out_the_shared_interval(self):
        sock = TimestampedSocket()

        commands.handle_ping_request(sock, "alice", "#chan")
        started = time.monotonic()
        commands.handle_ping_request(sock, "bob", "#chan")
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.08,
                                "a second !ping was not paced against the "
                                "first - the shared clock was never asked")

    def test_it_shares_the_clock_with_the_ordinary_queue(self):
        """The property the whole fix rests on: !ping and an ordinary queued
        reply must contend for the SAME budget, not two that sum - modelled
        by reserving the clock from outside and checking !ping still waits
        for it."""
        runtime.outbound_pacer.wait_for_slot(config.MSG_DELAY)
        sock = TimestampedSocket()

        started = time.monotonic()
        commands.handle_ping_request(sock, "alice", "#chan")
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.08,
                                "!ping sent immediately even though another "
                                "lane had just claimed the shared slot")

    def test_the_measurement_starts_after_the_wait_not_before(self):
        """#425's own honesty requirement: ping_start_time must be stamped
        once the wait is over, or every reported latency would be inflated
        by however long the socket happened to be busy - the exact number
        the command exists to measure would then be lying about the network
        to hide the pacer instead."""
        runtime.outbound_pacer.wait_for_slot(config.MSG_DELAY)
        sock = TimestampedSocket()
        before_call = time.time()

        commands.handle_ping_request(sock, "alice", "#chan")

        self.assertGreaterEqual(config.ping_start_time, before_call)
        self.assertLessEqual(config.ping_start_time, time.time())

    def test_the_probe_still_reaches_the_socket(self):
        """Control: pacing it must not accidentally swallow the send."""
        sock = TimestampedSocket()

        commands.handle_ping_request(sock, "alice", "#chan")

        self.assertEqual(len(sock.sent), 1)
        self.assertIn(b"PING :OSERVE_LATENCY_CHECK", sock.sent[0][1])


def queue_mgr_source():
    with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "queue_mgr.py"), encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    unittest.main()
