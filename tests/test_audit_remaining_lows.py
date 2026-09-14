"""Three low-severity findings from the audit, each with a real consequence.

None of them is dramatic on its own. What they have in common is that the
failure is silent: memory that grows with nothing logged, a truncated file
recorded as a completed send, and a record quietly replaced by a worse one.
"""

import io
import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheReadBufferCannotGrowForever(DCCoreTestCase):
    """`buffer += chunk` had no ceiling. A peer that never sends CRLF - an
    on-path attacker, or a PORT pointed at something that is not an ircd -
    grows it until the OOM reaper takes the daemon.

    The read loop cannot notice on its own: with no complete lines, no
    per-line handler ever runs, so nothing in the process gets a chance to
    object.
    """

    def test_a_normal_line_is_unaffected(self):
        leftover, lines = irc.take_complete_lines(b"", b"PING :abc\r\n")

        self.assertEqual(lines, ["PING :abc"])
        self.assertEqual(leftover, b"")

    def test_a_partial_line_is_still_held_for_the_next_chunk(self):
        """The whole point of the function: a chunk boundary inside a line,
        and inside a multi-byte character, must not lose anything."""
        leftover, lines = irc.take_complete_lines(b"", b"PING :ab")
        self.assertEqual(lines, [])

        leftover, lines = irc.take_complete_lines(leftover, b"c\r\n")

        self.assertEqual(lines, ["PING :abc"])

    def test_an_unterminated_flood_is_discarded(self):
        flood = b"x" * (irc.MAX_PENDING_LINE_BYTES + 1)

        leftover, lines = irc.take_complete_lines(b"", flood)

        self.assertEqual(lines, [])
        self.assertEqual(leftover, b"")

    def test_it_does_not_grow_across_repeated_chunks(self):
        """The real shape: no single chunk is large, the accumulation is."""
        leftover = b""
        for _ in range(200):
            leftover, _lines = irc.take_complete_lines(leftover, b"y" * 1024)

        self.assertLessEqual(len(leftover), irc.MAX_PENDING_LINE_BYTES)

    def test_the_ceiling_is_far_above_a_real_irc_line(self):
        """512 bytes by RFC 1459, plus at most 8191 of IRCv3 tags. The cap
        bounds memory; it must not police line length."""
        self.assertGreater(irc.MAX_PENDING_LINE_BYTES, 512 + 8191)

    def test_a_long_but_terminated_line_still_arrives(self):
        long_line = b"PRIVMSG #chan :" + b"z" * 8000 + b"\r\n"

        _leftover, lines = irc.take_complete_lines(b"", long_line)

        self.assertEqual(len(lines), 1)
        self.assertIn("z" * 8000, lines[0])


class ASendIsOnlyCompleteWhenItIsAllThere(DCCoreTestCase):
    """The send loop ends on local EOF, which says the file stopped giving
    bytes - not that it gave as many as the handshake promised.

    The receiver uses the advertised size to decide when the transfer is done,
    so a short send leaves it waiting for bytes that are never coming. Before
    this the short send was recorded as COMPLETE: counted in the totals,
    credited to the download counter, and the queue row deleted, so nothing
    would retry it.
    """

    def test_the_completion_test_compares_against_the_advertised_size(self):
        """Source-scoped to the send function, because the alternative is a
        live socket pair and a file mutated mid-transfer - and the defect is
        precisely which expression assigns transfer_completed."""
        import inspect

        source = inspect.getsource(dcc.start_dcc_send)

        self.assertIn("transfer_completed = bytes_sent >= file_size", source)
        self.assertNotIn("transfer_completed = True", source)

    def test_a_short_send_is_reported(self):
        """Silence is what made this survivable: the operator saw a normal
        completion line."""
        import inspect

        source = inspect.getsource(dcc.start_dcc_send)

        self.assertIn("before the file ended", source)


class TheSpeedRecordSurvivesConcurrentFinishes(DCCoreTestCase):
    """MAX_DCC_SLOTS transfers finish concurrently, and each calls
    update_speed_record() from its own thread.

    The read and the write used to be two separate lock acquisitions with the
    comparison between them, so two transfers could both read the old record,
    both decide they had beaten it, and the SLOWER one save last - replacing a
    5 MB/s record with a 1.2 MB/s one. Losing a record is not corruption, but
    it is the one number in the advert an operator cannot get back.
    """

    def test_the_higher_of_two_wins(self):
        db.save_speed_record(1_000_000)

        db.raise_speed_record_to(5_000_000)
        db.raise_speed_record_to(1_200_000)

        self.assertEqual(db.get_speed_record(), 5_000_000)

    def test_a_lower_sample_does_not_rewrite_it(self):
        db.save_speed_record(5_000_000)

        returned = db.raise_speed_record_to(1_200_000)

        self.assertEqual(returned, 5_000_000)
        self.assertEqual(db.get_speed_record(), 5_000_000)

    def test_the_first_sample_sets_it(self):
        self.assertEqual(db.raise_speed_record_to(2048), 2048)

    def test_a_nonsense_sample_is_refused_without_touching_the_file(self):
        db.save_speed_record(4242)

        self.assertEqual(db.raise_speed_record_to("fast"), 4242)
        self.assertEqual(db.get_speed_record(), 4242)

    def test_a_slow_reader_cannot_overwrite_a_record_set_meanwhile(self):
        """The race, made DETERMINISTIC.

        Threads alone do not reliably hit the window - it is a file read wide,
        and the 40-thread test below passed against the broken version. So
        this holds one caller inside its read while another runs to
        completion.

        The LOWER sample is the one paused, which is what discriminates: with
        the read inside the lock the second caller waits, sees the first
        result and compares against it. With the read outside, both see the
        old value and the slower one writes last - replacing a record that had
        just been set.
        """
        db.save_speed_record(0)
        inside_read = threading.Event()
        may_finish = threading.Event()
        real_read = db._read_speed_record_unlocked

        # Keyed to the slow thread. The patch is module-global, so a version
        # that paused unconditionally paused the FAST caller too - both then
        # waited on the same event, were released together, and the order was
        # nondeterministic. That version passed against the broken code.
        slow_thread_name = "slow-speed-record-caller"

        def pausing_read():
            value = real_read()
            if threading.current_thread().name == slow_thread_name:
                inside_read.set()
                may_finish.wait(5)
            return value

        slow_result = []

        def slow_caller():
            slow_result.append(db.raise_speed_record_to(1_200_000))

        db._read_speed_record_unlocked = pausing_read
        self.addCleanup(setattr, db, "_read_speed_record_unlocked", real_read)

        slow = threading.Thread(target=slow_caller, name=slow_thread_name)
        slow.start()
        self.assertTrue(inside_read.wait(5), "the slow caller never read")

        fast = threading.Thread(
            target=lambda: db.raise_speed_record_to(5_000_000))
        fast.start()

        # Give the fast caller a moment to get its read in. With the read
        # INSIDE the lock it cannot - it blocks until the slow caller is
        # released - and this simply costs the wait. With the read outside it
        # completes here, which is the interleaving being reproduced. The
        # fast thread must NOT be joined before the slow one is released, or
        # the correct version deadlocks the test rather than passing it.
        import time as time_mod

        deadline = time_mod.time() + 0.5
        while time_mod.time() < deadline and db.get_speed_record() != 5_000_000:
            time_mod.sleep(0.02)

        may_finish.set()
        slow.join(5)
        fast.join(5)

        self.assertEqual(db.get_speed_record(), 5_000_000,
                         "the slower sample overwrote a record set while it "
                         "was still reading the old one")

    def test_many_threads_finishing_together_keep_the_best(self):
        """Driven with real threads against the real file, which is what the
        two-acquisition version could not survive."""
        db.save_speed_record(0)
        speeds = [(i + 1) * 1000 for i in range(40)]
        best = max(speeds)

        def offer(value):
            db.raise_speed_record_to(value)

        threads = [threading.Thread(target=offer, args=(value,))
                   for value in speeds]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(db.get_speed_record(), best,
                         "a slower transfer finishing last discarded the "
                         "record a faster one had just set")


if __name__ == "__main__":
    unittest.main()
