"""The speed record: stored, read, advertised - and, until this change, never
written.

db.save_speed_record() has existed from the start and had tests of its own that
passed: it does write atomically to the right path. announce.py has read the
value into every channel advert since. Nothing ever sat between the two. The
only callers of the writer were the tests, so db.get_speed_record() returned 0
on every install and the advert published "Record: 0k/s" for the life of the
feature.

That is a gap no unit test could see, because every unit involved worked. It
needed something to ask whether the units were connected - which is the second
class in this file.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import stats_mgr  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class KeepingTheBestSpeed(DCCoreTestCase):
    """stats_mgr.update_speed_record() - the part that was missing."""

    def setUp(self):
        super().setUp()
        self.saved = []
        self.stored = 0
        self._real_get = db.get_speed_record
        self._real_save = db.save_speed_record
        self._real_raise = db.raise_speed_record_to
        db.get_speed_record = lambda: self.stored
        db.save_speed_record = self._record
        # The compare-and-set moved INTO db so the read and the write happen
        # under one lock - see db.raise_speed_record_to(). The stub keeps the
        # same in-memory semantics, so every assertion below still describes
        # what stats_mgr.update_speed_record() decides.
        db.raise_speed_record_to = self._raise_to
        self.addCleanup(setattr, db, "get_speed_record", self._real_get)
        self.addCleanup(setattr, db, "save_speed_record", self._real_save)
        self.addCleanup(setattr, db, "raise_speed_record_to", self._real_raise)

    def _record(self, value):
        self.saved.append(value)
        self.stored = value

    def _raise_to(self, candidate):
        try:
            speed = int(candidate)
        except (TypeError, ValueError):
            return self.stored
        if speed <= self.stored:
            return self.stored
        self._record(speed)
        return speed

    def test_a_faster_transfer_becomes_the_record(self):
        self.stored = 1000

        returned = stats_mgr.update_speed_record(5000, duration=10.0)

        self.assertEqual(self.saved, [5000])
        self.assertEqual(returned, 5000)

    def test_a_slower_transfer_leaves_the_record_alone(self):
        self.stored = 5000

        returned = stats_mgr.update_speed_record(1000, duration=10.0)

        self.assertEqual(self.saved, [], "a slower transfer overwrote the record")
        self.assertEqual(returned, 5000, "the record in force should come back")

    def test_matching_the_record_does_not_rewrite_it(self):
        """Equal is not better, and every write is a disk write."""
        self.stored = 5000

        stats_mgr.update_speed_record(5000, duration=10.0)

        self.assertEqual(self.saved, [])

    def test_the_first_ever_transfer_sets_the_record(self):
        """The case every install starts in, and the one that has never
        happened until now."""
        self.stored = 0

        returned = stats_mgr.update_speed_record(2048, duration=10.0)

        self.assertEqual(self.saved, [2048])
        self.assertEqual(returned, 2048)

    def test_a_transfer_too_short_to_time_cannot_set_a_record(self):
        """dcc.py floors the measured duration at 0.1s and takes it from a
        start_time that may not be bound. Both produce a rate that is ten times
        the file size and means nothing - and a record is permanent."""
        self.stored = 0

        returned = stats_mgr.update_speed_record(99_000_000, duration=0.1)

        self.assertEqual(self.saved, [], "a 0.1s sample was allowed to set the record")
        self.assertEqual(returned, 0)

    def test_a_sample_with_no_duration_is_still_accepted(self):
        """The duration is a filter, not a requirement - a caller that does not
        measure one still gets the comparison."""
        self.stored = 100

        stats_mgr.update_speed_record(900)

        self.assertEqual(self.saved, [900])

    def test_nonsense_samples_are_refused_rather_than_stored(self):
        """A record is permanent and public, so anything unparseable has to be
        dropped rather than written and lived with."""
        self.stored = 700

        for bad in (0, -1, None, "fast", ""):
            with self.subTest(sample=bad):
                self.assertEqual(stats_mgr.update_speed_record(bad, duration=10.0), 700)

        self.assertEqual(self.saved, [])


class EveryPersistedValueHasAWriterInTheDaemon(unittest.TestCase):
    """The class of bug this file exists for.

    db.py's writers were each correct and each tested. One of them was simply
    never reached from the running daemon, so the value it maintained stayed at
    its default forever while a reader published it.

    A function is "reachable" here if a module outside db.py and tests/ calls
    it, or if another db.py function that is itself reachable calls it - which
    is how save_advanced_stats() legitimately has no direct caller of its own:
    update_stats_on_complete() wraps it and dcc.py calls that.

    WHAT THIS DOES NOT CATCH, VERIFIED RATHER THAN ASSUMED

    Reachability stops at the first daemon module that mentions the name. It
    does not ask whether THAT function is itself called. So a writer whose only
    caller is dead code still reads as reachable.

    Checked both ways while writing this: against the state that shipped -
    nothing outside tests/ calling the writer at all - it fails, which is the
    bug it is for. Remove only dcc.py's call and it passes, because
    stats_mgr.update_speed_record() still names the writer. Catching that
    second case means a real call graph across the whole daemon, which is a
    bigger tool than this file should be; the unit tests above cover that the
    wrapper behaves, and this covers that the chain is not severed at the end.
    """

    @staticmethod
    def db_source():
        with io.open(os.path.join(REPO_ROOT, "db.py"), encoding="utf-8") as handle:
            return handle.read()

    def writers(self):
        """db.py functions that put something on disk."""
        import ast
        tree = ast.parse(self.db_source())
        found = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            body = ast.dump(node)
            if "_atomic_write" in body or "'w'" in body or '"w"' in body:
                found.append(node.name)
        return found

    def daemon_modules(self):
        return [f for f in sorted(os.listdir(REPO_ROOT))
                if f.endswith(".py") and f not in ("db.py",)]

    def reachable(self):
        """Every db.py function the daemon can actually get to."""
        source = self.db_source()
        direct = set()
        for filename in self.daemon_modules():
            with io.open(os.path.join(REPO_ROOT, filename), encoding="utf-8") as handle:
                text = handle.read()
            for name in re.findall(r"def (\w+)\(", source):
                if re.search(r"\b%s\s*\(" % re.escape(name), text):
                    direct.add(name)

        # Then anything those call, transitively, inside db.py itself.
        bodies = {}
        import ast
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef):
                bodies[node.name] = {n.func.id for n in ast.walk(node)
                                     if isinstance(n, ast.Call)
                                     and isinstance(n.func, ast.Name)}
        seen, queue = set(direct), list(direct)
        while queue:
            name = queue.pop()
            for callee in bodies.get(name, ()):
                if callee in bodies and callee not in seen:
                    seen.add(callee)
                    queue.append(callee)
        return seen

    def test_no_writer_is_stranded(self):
        reachable = self.reachable()

        stranded = sorted(name for name in self.writers() if name not in reachable)

        self.assertEqual(
            stranded, [],
            "these db.py functions write to disk but nothing in the daemon can "
            "reach them, so whatever they maintain stays at its default while "
            "readers publish it: " + ", ".join(stranded))

    def test_the_scan_finds_the_writers_it_is_meant_to_check(self):
        """Fixture invariant: a scan that matched nothing would pass this file
        while checking nothing at all."""
        writers = self.writers()

        self.assertGreaterEqual(len(writers), 3,
                                f"only {len(writers)} db.py writer(s) found; the "
                                f"scan has probably stopped recognising them")
        self.assertIn("save_speed_record", writers,
                      "the writer this test was written for is not being seen")


class TheClockStopsWhenTheBytesDo(DCCoreTestCase):
    """"i feel it slower than mirc omenserve even with 64kb" - and the
    transfer was never slow. The NUMBER was.

    The duration was measured at the very end of start_dcc_send(), after two
    deliberate pauses: 1.5 seconds for the receiver to close its file calmly,
    and another half-second before the statistics write. Two seconds of
    settling, counted as transfer time, against files that mostly take less
    than that.

    A loopback benchmark of the send loop itself reaches ~950 MB/s at 64 KB on
    this machine, so the loop was never the limit either.
    """

    def test_the_settling_pause_is_not_counted(self):
        """Read out of the source: driving a real transfer needs a peer on a
        socket. Asserted as an ORDER, because a timestamp taken in the right
        place and then not used would pass a check for either half alone."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            code = handle.read()

        stamped = code.index("transfer_finished_at = time.time()")
        first_pause = code.index("time.sleep(1.5)", stamped)
        used = code.index("_ended = (transfer_finished_at", stamped)

        self.assertLess(stamped, first_pause,
                        "the clock is stopped after the settling pause")
        self.assertLess(first_pause, used,
                        "fixture invariant: the pause should sit between the "
                        "stamp and its use, or this proves nothing")

    def test_the_duration_no_longer_reads_the_wall_clock_at_the_end(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            code = handle.read()

        self.assertNotIn(
            "acute_duration = time.time() - (start_time", code,
            "the duration is measured at the end of the function again, so "
            "every pause between the last byte and here is counted as "
            "transfer time")

    def test_what_the_old_arithmetic_did_to_a_real_file(self):
        """Not a test of the code - a test of the claim, so the size of the
        error is written down somewhere it cannot quietly stop being true.
        A 10 MB file at 46 MB/s takes 0.22s; two seconds of settling made it
        report a tenth of its real rate."""
        megabytes, real_rate, settling = 10.0, 46.0, 2.0
        honest_seconds = megabytes / real_rate

        reported = megabytes / (honest_seconds + settling)

        self.assertLess(reported, real_rate / 9,
                        "the settling pause no longer dominates a small "
                        "file, so this note is out of date")


if __name__ == "__main__":
    unittest.main()
