"""The socket send buffer is not left at a 64 KB ceiling on Windows.

MEASURED IN A BETA, and it cost a factor of eight. Same friend, same machine,
same link:

    OmenServe : 30.4 MB/s
    DCCore    : 2.95 / 2.98 / 3.00 / 3.01 MB/s   (four files, different sizes)

Identical every time, because it was arithmetic rather than congestion. TCP
cannot have more bytes in flight than the send buffer holds, so throughput is
bounded by SO_SNDBUF / round-trip-time. The default buffer on that machine was
exactly 65,536 bytes.

Setting the packet size to 4 KB made it WORSE - 1.6 MB/s - and fitting both
measurements gives the whole picture:

    effective ceiling  : 3.19 MB/s
    fixed cost / block : 1.27 ms
    implied RTT        : 20.6 ms      64 KB / 20.6 ms = 3.19 MB/s

An entirely ordinary internet round trip. Raising DCC_SEND_BUFFER to 1 MB took
the same transfer to 23.9 and 24.7 MB/s.

WHY PER-PLATFORM RATHER THAN SIMPLY A NEW DEFAULT. The old behaviour was
"never set it unless asked", justified by SO_SNDBUF disabling the OS's own
auto-tuning. That is sound on Linux, where tcp_wmem grows the buffer to fit
the connection and pinning it would be a downgrade on exactly the long-haul
links that need it most. It does not hold on Windows, where "leave it alone"
means a fixed 64 KB. Neither platform's answer is the other's mistake, so the
default differs and an explicit setting still wins on both.
"""

import os
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import platform_compat  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class RecordingSocket:
    """Remembers setsockopt calls instead of owning a real socket."""

    def __init__(self, fail=False):
        self.options = []
        self.fail = fail

    def setsockopt(self, level, option, value):
        if self.fail:
            raise OSError("refused")
        self.options.append((level, option, value))

    def send_buffer(self):
        for level, option, value in self.options:
            if (level, option) == (socket.SOL_SOCKET, socket.SO_SNDBUF):
                return value
        return None


class TheArithmeticThatStartedThis(unittest.TestCase):
    """Not a test of our code - a statement of why the default matters, kept
    executable so the reasoning cannot rot into a comment nobody checks."""

    def test_a_64kb_window_over_an_ordinary_rtt_is_about_three_megabytes(self):
        ceiling = 65536 / 0.0206          # bytes per second at a 20.6 ms RTT

        self.assertAlmostEqual(ceiling / 1e6, 3.18, places=1)

    def test_a_one_megabyte_window_is_not_the_limit_on_such_a_link(self):
        ceiling = (1024 * 1024) / 0.0206

        self.assertGreater(ceiling / 1e6, 20)


class TheDefaultDependsOnThePlatform(DCCoreTestCase):

    def applied(self, configured):
        self.set_config(DCC_SEND_BUFFER=configured)
        conn = RecordingSocket()
        dcc._apply_send_buffer(conn, log=lambda *a, **k: None)
        return conn.send_buffer()

    def test_an_explicit_value_is_used_on_every_platform(self):
        self.assertEqual(self.applied(262144), 262144)

    def test_an_explicit_small_value_is_still_honoured(self):
        """A deliberate choice, even a low one, is not ours to override."""
        self.assertEqual(self.applied(8192), 8192)

    def test_zero_means_the_platform_default(self):
        expected = 4 * 1024 * 1024 if platform_compat.IS_WINDOWS else None

        self.assertEqual(self.applied(0), expected)

    def test_windows_gets_a_buffer_far_above_the_64kb_ceiling(self):
        """The whole point. 64 KB is what produced 3 MB/s."""
        if not platform_compat.IS_WINDOWS:
            self.skipTest("the default only applies on Windows")

        self.assertGreater(self.applied(0), 65536 * 4)

    def test_linux_is_left_to_its_own_auto_tuning(self):
        """tcp_wmem grows the buffer to fit the connection, and setting
        SO_SNDBUF explicitly turns that off - a downgrade on exactly the
        long-haul links this is meant to help."""
        if platform_compat.IS_WINDOWS:
            self.skipTest("this asserts the non-Windows branch")

        self.assertIsNone(self.applied(0))

    def test_the_constant_matches_the_platform_it_ran_on(self):
        """Guard on the guard: both branches above are skipped on one
        platform each, so this pins the value itself."""
        if platform_compat.IS_WINDOWS:
            self.assertEqual(dcc._DEFAULT_SEND_BUFFER, 4 * 1024 * 1024)
        else:
            self.assertEqual(dcc._DEFAULT_SEND_BUFFER, 0)

    def test_the_default_is_written_as_a_platform_choice(self):
        """The two branch tests above each skip on the other platform, so a
        change to "1 MB everywhere" is invisible when the suite runs on
        Windows - which is where this was measured, and where someone
        adjusting it would be working. Pinned as source so both platforms
        catch it.

        Pinning the EXPRESSION rather than the number: the point is that the
        answer differs by platform, and a value that stopped being
        conditional would be wrong on Linux however large it was."""
        import io

        with io.open(os.path.join(REPO_ROOT, "dcc.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("_DEFAULT_SEND_BUFFER = 4 * 1024 * 1024 "
                      "if platform_compat.IS_WINDOWS else 0", source)


class ABiggerBufferCannotInflateTheSpeedRecord(DCCoreTestCase):
    """The one thing raising this default makes load-bearing.

    sendall() returns once the bytes are in the KERNEL, not once the peer has
    them. A file that fits entirely inside the send buffer therefore "sends"
    in almost no time, and dividing its size by that gives a number with no
    relationship to the link. The window of files this applies to is exactly
    the buffer size - so it grew from under 1 MB to under 4 MB, which is most
    single tracks.

    stats_mgr.MIN_RECORD_SECONDS already refuses a sample measured over less
    than a second, and it predates all of this. These tests tie that guard to
    THIS reason, so the buffer can be changed again later without anyone
    having to re-derive why the floor matters - which is the only way a
    guard like it survives.

    The record is what the channel advert publishes, so a bogus one is not a
    private mistake."""

    def test_a_transfer_that_vanished_into_the_buffer_sets_no_record(self):
        import stats_mgr

        before = stats_mgr.update_speed_record(5_000_000, duration=10.0)
        absurd = 4 * 1024 * 1024 * 100     # a 4 MB file "sent" in 10 ms

        after = stats_mgr.update_speed_record(absurd, duration=0.01)

        self.assertEqual(after, before)

    def test_a_real_transfer_still_sets_one(self):
        """Guard on the guard: a floor that refused everything would pass the
        test above and quietly stop the record ever moving."""
        import stats_mgr

        before = stats_mgr.update_speed_record(1_000_000, duration=10.0)

        after = stats_mgr.update_speed_record(9_000_000, duration=10.0)

        self.assertGreater(after, before)

    def test_the_floor_is_longer_than_a_buffer_takes_to_fill(self):
        """A local memcpy of a few megabytes is microseconds. The floor only
        has to be long enough that no buffer-sized write can clear it."""
        import stats_mgr

        self.assertGreaterEqual(stats_mgr.MIN_RECORD_SECONDS, 1.0)


class ItNeverCostsATransfer(DCCoreTestCase):
    """A socket option is not worth failing a send that would otherwise
    work - the kernel may refuse it or round it, either way the bytes should
    still move."""

    def test_a_refused_option_does_not_raise(self):
        self.set_config(DCC_SEND_BUFFER=1048576)

        dcc._apply_send_buffer(RecordingSocket(fail=True),
                               log=lambda *a, **k: None)

    def test_a_refused_default_does_not_raise_either(self):
        """The default path reaches the same setsockopt, and now runs on
        installs that never configured anything."""
        self.set_config(DCC_SEND_BUFFER=0)

        dcc._apply_send_buffer(RecordingSocket(fail=True),
                               log=lambda *a, **k: None)

    def test_a_nonsense_setting_is_ignored_rather_than_crashing(self):
        self.set_config(DCC_SEND_BUFFER="not a number")
        conn = RecordingSocket()

        dcc._apply_send_buffer(conn, log=lambda *a, **k: None)

        self.assertIsNone(conn.send_buffer())


if __name__ == "__main__":
    unittest.main()
