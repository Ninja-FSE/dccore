"""A hung audio read is a stall the watchdog can see (#968).

A rebuild with LIST_SHOW_AUDIO_INFO reads length and quality many files at a
time. When a network mount stops answering, every read in flight blocks. The
read loop still reported progress every second whether anything had finished
or not, so the progress file kept moving, the watchdog that stops a stalled
rebuild after LIST_UPDATE_STALL_SECONDS never saw a stall, the budget only
stopped new reads, and the rebuild never ended - every later !update refused
as already running, until a restart.

Now progress is reported at the start and then only when a read completes.
"""

import threading
import unittest

from tests import support  # noqa: F401  (path setup)

import audio_info  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_list_says_how_long_and_how_good as long_and_good  # noqa: E402

EMPTY_ROUNDS = 5


class AHungReadIsAStall(long_and_good.FileCase):
    # The fixture's own helper, borrowed rather than inherited so its tests
    # do not run again here.
    pending = long_and_good.ReadingManyAtOnce.pending

    def test_a_second_with_nothing_finished_is_not_progress(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def reader(path, size=None):
            release.wait(30)
            return audio_info.read(path, size)

        self.reader = reader
        cache = self.pending(1)

        # Each call is one of the loop's one-second waits. The first few find
        # nothing finished - the mount is not answering - then it answers.
        rounds = []
        real_wait = audio_info.wait

        def wait(futures, timeout=None, return_when=None):
            rounds.append(timeout)
            if len(rounds) <= EMPTY_ROUNDS:
                return set(), set(futures)
            release.set()
            return real_wait(futures, timeout=timeout, return_when=return_when)

        audio_info.wait = wait
        self.addCleanup(setattr, audio_info, "wait", real_wait)

        seen = []
        cache.read_pending(workers=1, progress=lambda done, total: seen.append((done, total)))

        self.assertGreater(len(rounds), EMPTY_ROUNDS, "the empty seconds did happen")
        self.assertEqual(seen, [(0, 1), (1, 1)],
                         "the start, then the one read - nothing for the seconds in between")
        self.assertEqual(cache.read_count, 1)


if __name__ == "__main__":
    unittest.main()
