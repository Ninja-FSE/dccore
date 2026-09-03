"""The live transfer rate, and who is allowed to read it.

Until now this was computed inside announce.py's advert cycle, as a local, and
thrown away when the line was sent. Two things were wrong with that as soon as
anything else wanted the number:

  * The advert fires every ANNOUNCE_INTERVAL - 300 seconds by default. Fine for
    a channel line, useless for a dashboard tile.
  * bytes_sent is a LIFETIME counter, so a rate is bytes moved since the last
    observation over the time since it. SAMPLING CONSUMES THE WINDOW. Two
    callers each taking their own sample would each see part of the movement,
    and both would report a fraction of the real speed.

So there is one sampler, cached for a second, and the figure is left in
runtime where anything can read it - including webserver.py, which must not
import the daemon and has a test of its own pinning that.
"""

import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import runtime  # noqa: E402
import stats_mgr  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class SamplingTheRate(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.active_transfers = []
        runtime.live_speed_bps = 0
        runtime.live_speed_sampled_at = 0.0

    def transfer(self, sent):
        row = {"user": "dave", "file": "x.flac", "bytes_sent": sent}
        config.active_transfers.append(row)
        return row

    def test_no_transfers_is_zero(self):
        self.assertEqual(stats_mgr.live_speed(now=100.0), 0)

    def test_a_first_sighting_has_no_window_to_measure(self):
        """One observation is a position, not a rate. Reporting anything here
        would mean dividing a lifetime counter by a window that started when we
        happened to look, which over-reports by up to double."""
        self.transfer(5_000_000)

        self.assertEqual(stats_mgr.live_speed(now=100.0), 0)

    def test_two_observations_a_second_apart_give_the_rate(self):
        row = self.transfer(1_000_000)
        stats_mgr.live_speed(now=100.0)          # first sighting
        row["bytes_sent"] = 3_000_000            # 2 MB moved

        self.assertEqual(stats_mgr.live_speed(now=102.0), 1_000_000,
                         "2MB over 2s should read as 1MB/s")

    def test_the_average_is_over_contributors_not_slots(self):
        """A transfer skipped for want of a window must not drag the mean
        toward zero - it has no measurement, which is not the same as a
        measurement of nothing."""
        moving = self.transfer(1_000_000)
        stats_mgr.live_speed(now=100.0)

        self.transfer(0)                          # joins late, no window yet
        moving["bytes_sent"] = 2_000_000          # 1 MB over 1s

        self.assertEqual(stats_mgr.live_speed(now=101.0), 1_000_000,
                         "the newcomer halved the average")

    def test_only_forward_movement_counts(self):
        """A counter that went backwards means the row was replaced, not that
        the transfer ran in reverse."""
        row = self.transfer(5_000_000)
        stats_mgr.live_speed(now=100.0)
        row["bytes_sent"] = 1_000_000

        self.assertEqual(stats_mgr.live_speed(now=102.0), 0)


    def test_a_short_window_on_one_transfer_is_skipped(self):
        """The per-transfer window guard, reached deliberately.

        Normally the cache makes this unreachable: it forces at least a second
        between samples, and every sample stamps every transfer, so a
        transfer's window is always at least that long. An earlier version of
        this test passed for that reason rather than for the right one - it
        asked at now=100.5, the cache answered, and the guard was never
        involved.

        A row can still carry a fresher stamp than the last sample: one that
        was being written while a sample ran, or one left over across a
        !rehash. So the guard stays, and this reaches it by stamping the row
        directly while leaving the cache stale.
        """
        row = {"user": "dave", "file": "x.flac", "bytes_sent": 9_000_000,
               "_speed_bytes": 1_000_000, "_speed_time": 101.8}
        config.active_transfers.append(row)
        runtime.live_speed_sampled_at = 100.0     # cache is 2s stale, will sample
        runtime.live_speed_bps = 12345            # and would be overwritten

        value = stats_mgr.live_speed(now=102.0)

        self.assertEqual(value, 0,
                         "8MB over a 0.2s window was treated as a real rate")

class OneSamplerManyReaders(DCCoreTestCase):
    """The property that made this a shared function rather than a copied one.

    Sampling consumes the window. If the advert and the dashboard each took
    their own sample a moment apart, the second would measure the sliver of
    movement since the first and report a speed several times too low - and
    which of them got the real number would depend on who asked first.
    """

    def setUp(self):
        super().setUp()
        config.active_transfers = []
        runtime.live_speed_bps = 0
        runtime.live_speed_sampled_at = 0.0

    def test_a_second_caller_inside_the_window_gets_the_same_answer(self):
        row = {"user": "dave", "file": "x.flac", "bytes_sent": 1_000_000}
        config.active_transfers.append(row)
        stats_mgr.live_speed(now=100.0)
        row["bytes_sent"] = 3_000_000

        first = stats_mgr.live_speed(now=102.0)
        row["bytes_sent"] = 3_010_000            # a sliver more
        second = stats_mgr.live_speed(now=102.3)

        self.assertEqual(first, 1_000_000)
        self.assertEqual(second, first,
                         "the second caller re-sampled and saw a fraction of "
                         "the movement instead of the cached figure")

    def test_the_sample_is_published_where_others_can_read_it(self):
        row = {"user": "dave", "file": "x.flac", "bytes_sent": 1_000_000}
        config.active_transfers.append(row)
        stats_mgr.live_speed(now=100.0)
        row["bytes_sent"] = 2_000_000

        value = stats_mgr.live_speed(now=101.0)

        self.assertEqual(runtime.live_speed_bps, value)
        self.assertEqual(runtime.live_speed_sampled_at, 101.0)


class TheDashboardCanReadItWithoutTheDaemon(unittest.TestCase):
    """Why the figure lives in runtime rather than being returned only to its
    caller.

    webserver.py imports `list` lazily so that importing it does not drag in
    oserve/dcc/announce, and tests/test_import_graph.py pins that. The rate is
    sampled where dcc.queue_lock is available and published somewhere the
    dashboard can read without any of that - so this checks the reading half
    stays possible, in a clean interpreter where the daemon is not already
    loaded.
    """

    def test_reading_the_rate_pulls_in_no_daemon_modules(self):
        code = (
            "import sys\n"
            "import webserver, runtime\n"
            "speed = runtime.live_speed_bps\n"
            "assert isinstance(speed, int), speed\n"
            "heavy = [m for m in ('dcc', 'oserve', 'announce', 'queue_mgr')\n"
            "         if m in sys.modules]\n"
            "print(','.join(heavy) or 'NONE')\n"
        )
        result = subprocess.run([sys.executable, "-c", code],
                                cwd=REPO_ROOT, capture_output=True,
                                text=True, timeout=120)

        self.assertEqual(result.returncode, 0, result.stderr.strip()[-1500:])
        self.assertEqual(
            result.stdout.strip(), "NONE",
            "reading the live rate from the dashboard's side pulled in the "
            "daemon: " + result.stdout.strip())



class TheRateIsKeptCurrent(unittest.TestCase):
    """Something has to sample, or every reader sees the same stale figure.

    The advert used to be the only caller and fires every ANNOUNCE_INTERVAL -
    300 seconds by default. A dashboard tile showing a five-minute-old transfer
    rate is worse than showing none, because it looks live.

    This is the same shape as the speed record in #119: a function that is
    correct, tested, and called by nothing. Unit tests cannot see that, so it
    needs asking directly.
    """

    def test_the_queue_worker_samples_the_rate(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo_root, "queue_mgr.py"), encoding="utf-8") as handle:
            source = handle.read()

        calls = [line.strip() for line in source.splitlines()
                 if "live_speed(" in line and not line.strip().startswith("#")]

        self.assertTrue(
            calls,
            "queue_mgr.py no longer samples the live rate. It is the daemon's "
            "heartbeat and the only loop running often enough to keep the "
            "figure current; without it the value is only as fresh as the last "
            "advert, which is ANNOUNCE_INTERVAL seconds old.")

    def test_the_advert_no_longer_samples_it_separately(self):
        """One sampler. Two would each measure part of the movement and both
        would report a fraction of the real speed."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo_root, "announce.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertNotIn(
            "_speed_time", source,
            "announce.py is sampling the rate itself again - it should call "
            "stats_mgr.live_speed() and let the cache decide whether that is a "
            "fresh sample or the last one")


if __name__ == "__main__":
    unittest.main()
