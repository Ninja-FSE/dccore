"""Every suite run left about 236 entries in the temp folder (#1149).

Tests made temp directories with tempfile.mkdtemp() and never removed them,
or removed them with shutil.rmtree(..., ignore_errors=True) in tearDown() -
which runs BEFORE every addCleanup(), so before a test's own cleanup had
closed the sqlite file it opened there. On Windows an open file cannot be
deleted, ignore_errors swallowed that, and the directory stayed. 232,000
entries had built up on one machine, and listing that folder had become slow
enough to slow every test that starts a child process from it.

tests/support.py now has remove_tree(), which retries after closing what
the suite caches and collecting connections nobody holds; temp_dir(test)
and DCCoreTestCase.make_temp_dir(), which register it; and DCCoreTestCase
removes its own redirect directories in its last cleanup instead of in
tearDown(). These tests hold that machinery to its promises. Preflight's
side - every pass runs with a temp folder of its own and fails on what it
leaves there - is covered in tests/test_preflight_names_what_a_pass_leaves_in_temp.py.
"""

import gc
import io
import os
import sqlite3
import sys
import tempfile
import unittest
import warnings

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list_index  # noqa: E402

from tests.support import DCCoreTestCase, remove_tree, temp_dir  # noqa: E402


def run_case(case_class):
    """Run one inner TestCase quietly; return its result."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(case_class)
    return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)


class _Cycle:
    """Holds a connection in a reference cycle: nobody can reach it, but
    only the garbage collector will ever close it."""

    def __init__(self, conn):
        self.conn = conn
        self.me = self


class RemoveTree(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="dccore-remove-tree-")
        # If remove_tree() is what is broken, do not leave the evidence behind.
        self.addCleanup(self._last_resort)

    def _last_resort(self):
        list_index.reset_for_tests()
        gc.collect()
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_a_plain_directory_goes(self):
        os.makedirs(os.path.join(self.folder, "a", "b"))
        with io.open(os.path.join(self.folder, "a", "b", "c.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")

        remove_tree(self.folder)

        self.assertFalse(os.path.exists(self.folder))

    def test_a_missing_directory_is_nothing(self):
        remove_tree(os.path.join(self.folder, "never-made"))
        remove_tree("")
        remove_tree(None)

    def test_a_sqlite_file_nobody_holds_but_nobody_closed_does_not_keep_it(self):
        """The case that left the directories on Windows: a connection that
        is unreachable but not yet collected still holds its file open."""
        gc.disable()
        self.addCleanup(gc.enable)
        conn = sqlite3.connect(os.path.join(self.folder, "held.db"))
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        _Cycle(conn)
        del conn

        with warnings.catch_warnings():
            # Collecting it says "unclosed database", which is the point.
            warnings.simplefilter("ignore", ResourceWarning)
            remove_tree(self.folder)

        self.assertFalse(os.path.exists(self.folder), "a file nobody holds kept its directory")

    def test_the_index_connection_the_suite_caches_does_not_keep_it(self):
        self.addCleanup(setattr, config, "LIST_INDEX_FILE", getattr(config, "LIST_INDEX_FILE", None))
        config.LIST_INDEX_FILE = os.path.join(self.folder, "idx.db")
        list_index.reset_for_tests()
        list_index.indexed_bots()   # opens and keeps the shared connection
        self.assertTrue(os.path.exists(config.LIST_INDEX_FILE))

        remove_tree(self.folder)

        self.assertFalse(os.path.exists(self.folder), "the cached index connection kept its directory")


class TheHelpersRemoveWhatTheyHandOut(unittest.TestCase):

    def test_temp_dir_is_gone_when_a_plain_test_ends(self):
        made = []

        class Inner(unittest.TestCase):
            def test_it(inner):
                made.append(temp_dir(inner, prefix="dccore-helper-"))
                self.assertTrue(os.path.isdir(made[0]))

        self.assertTrue(run_case(Inner).wasSuccessful())
        self.assertFalse(os.path.exists(made[0]))

    def test_temp_dir_is_gone_when_the_test_fails(self):
        made = []

        class Inner(unittest.TestCase):
            def test_it(inner):
                made.append(temp_dir(inner))
                inner.fail("on purpose")

        self.assertFalse(run_case(Inner).wasSuccessful())
        self.assertFalse(os.path.exists(made[0]))

    def test_make_temp_dir_outlives_the_tests_own_cleanups(self):
        """The test's own cleanup closes the file; only after that does the
        directory go. Removed any earlier, on Windows it stayed."""
        made = []
        seen_by_own_cleanup = []

        class Inner(DCCoreTestCase):
            def test_it(inner):
                folder = inner.make_temp_dir(prefix="dccore-helper-")
                made.append(folder)
                conn = sqlite3.connect(os.path.join(folder, "open.db"))
                conn.execute("CREATE TABLE t (x)")
                inner.addCleanup(conn.close)
                inner.addCleanup(lambda: seen_by_own_cleanup.append(os.path.isdir(folder)))

        self.assertTrue(run_case(Inner).wasSuccessful())
        self.assertEqual(seen_by_own_cleanup, [True], "the directory went before the test's own cleanups ran")
        self.assertFalse(os.path.exists(made[0]))

    def test_the_redirect_directory_outlives_the_tests_own_cleanups(self):
        """DCCoreTestCase's own per-test directory - the fetch history, the
        transfer record, the list index - is removed by its last cleanup,
        not by tearDown(), which runs before every cleanup."""
        made = []

        class Inner(DCCoreTestCase):
            def test_it(inner):
                made.append(inner._fetch_history_dir)
                conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
                conn.execute("CREATE TABLE t (x)")
                inner.addCleanup(conn.close)

        self.assertTrue(run_case(Inner).wasSuccessful())
        self.assertTrue(made[0].startswith(tempfile.gettempdir()))
        self.assertFalse(os.path.exists(made[0]), "the redirect directory was left behind")

    def test_a_second_setup_call_does_not_orphan_the_first(self):
        """tests/test_the_resume_handshake_takes_its_turn.py calls setUp() a
        second time on purpose; the first call's directories still go."""
        made = []

        class Inner(DCCoreTestCase):
            def test_it(inner):
                made.append(inner._fetch_history_dir)
                inner.setUp()
                made.append(inner._fetch_history_dir)

        self.assertTrue(run_case(Inner).wasSuccessful())
        self.assertEqual(len(set(made)), 2)
        self.assertEqual([path for path in made if os.path.exists(path)], [])

    def test_a_tree_goes_too_after_the_tests_own_cleanups(self):
        made = []

        class Inner(DCCoreTestCase):
            def test_it(inner):
                tree = inner.make_tree()
                made.append(tree.root)
                conn = sqlite3.connect(os.path.join(tree.lists, "open.db"))
                conn.execute("CREATE TABLE t (x)")
                inner.addCleanup(conn.close)

        self.assertTrue(run_case(Inner).wasSuccessful())
        self.assertFalse(os.path.exists(made[0]))


class TheChildScriptHasAFolderOfItsOwn(unittest.TestCase):
    """The interrupt tests ran a child script written loose into the temp
    folder, which made that folder the child's sys.path[0]: every import it
    made listed the whole temp folder first."""

    def test_the_sleeper_is_not_written_loose_into_the_temp_folder(self):
        with io.open(os.path.join(REPO_ROOT, "tests", "test_the_bot_can_be_stopped_without_its_window.py"),
                     encoding="utf-8") as handle:
            source = handle.read()
        run_child = source.split("    def run_child(self, mode):", 1)[1].split("\n    def ", 1)[0]

        self.assertIn('with tempfile.TemporaryDirectory(prefix="dccore-interrupt-") as folder:', run_child)
        self.assertIn('handle, path = tempfile.mkstemp(suffix=".py", dir=folder)', run_child)


if __name__ == "__main__":
    unittest.main()
