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
        db.get_speed_record = lambda: self.stored
        db.save_speed_record = self._record
        self.addCleanup(setattr, db, "get_speed_record", self._real_get)
        self.addCleanup(setattr, db, "save_speed_record", self._real_save)

    def _record(self, value):
        self.saved.append(value)
        self.stored = value

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


if __name__ == "__main__":
    unittest.main()
