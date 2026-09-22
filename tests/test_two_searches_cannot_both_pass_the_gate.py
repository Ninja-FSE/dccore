"""Two @find lines arriving together scan the list once, not twice.

THE DEFECT (#607)

Every @find runs on its own thread (irc.py starts one per line), and
execute_search()'s "one search at a time" guard was a check of
config.search_inprogress at the top of the function and a set of the same flag
some twenty lines later - with library.list_name_for_request()'s trip to
lists.json in between and no lock around any of it. Two @find lines dispatched
from the same recv() buffer both read False, both set True, and both walked
the master list at once - the exact cost runtime.list_count_lock's comment says
the design exists to avoid. The first to finish then cleared the flag while
the other was still running, so a third searcher was let in as well, and an
!update arriving then passed its "no scan running" check with a scan in
progress.

!update had the same shape on its side: it raised update_inprogress under
runtime.list_update_gate (#444) and then read and raised search_inprogress
OUTSIDE it, and its finally cleared search_inprogress unconditionally - with
PAUSE_ON_UPDATE off, that cleared a flag a running search owned.

THE FIX

Both check-and-sets are one step under runtime.list_update_gate, the one lock
that already existed for this flag pair, and each side clears only the flag it
raised. The refused searcher returns before execute_search()'s try, so its
finally never runs.

The concurrency test forces the interleaving rather than betting on it: both
searchers are held at the list lookup - inside the old window - until both
are there, and the scan itself is held until the test has seen the outcome.
"""

import ast
import io
import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket, silence_debug  # noqa: E402


class SearchCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.track = os.path.join(self.tree.music, "Song.flac")
        with io.open(self.track, "w", encoding="utf-8") as handle:
            handle.write("x" * 4096)
        self.set_config(FILE_DIRECTORY=self.tree.music,
                        LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
                        CHANNEL="#chan", PAUSE_ON_UPDATE=True,
                        search_inprogress=False, update_inprogress=False)

    def patch(self, module, name, replacement):
        real = getattr(module, name)
        setattr(module, name, replacement)
        self.addCleanup(setattr, module, name, real)
        return real

    def replies(self):
        return "".join(m for _u, m, *_ in self.oserve.queued)


class TwoSearchersArrivingTogether(SearchCase):

    def test_only_one_of_them_walks_the_list(self):
        """The defect itself. Both searchers are inside the old check-to-set
        window at once; exactly one may come out of it scanning."""
        reached_lookup = threading.Barrier(2, timeout=5)
        release_scan = threading.Event()
        scanning = []

        real_lookup = self.patch(library, "list_name_for_request", None)

        def lookup_then_wait_for_the_other(channel=None):
            name = real_lookup(channel)
            reached_lookup.wait()
            return name

        def held_scan(search_words, limit=None, name=None):
            scanning.append(name)
            release_scan.wait(5)
            return [{"line": "!DCCoreTest Song.flac"}], 1

        library.list_name_for_request = lookup_then_wait_for_the_other
        self.patch(list_mod, "find_latest_list", lambda name=None: self.track)
        self.patch(list_mod, "find_matching_entries", held_scan)

        threads = [threading.Thread(
                       target=list_mod.execute_search,
                       args=(RecordingSocket(), user, "song", "#chan"),
                       daemon=True)
                   for user in ("alice", "bob")]
        for thread in threads:
            thread.start()

        # Wait for the outcome, whichever it is: both scanning (the defect)
        # or one searcher refused and gone AND the other one scanning. The
        # refused one can return before the winner has reached the scan, so
        # "one thread gone" alone is too early to count the scanners (that
        # read 0 on a macOS runner).
        def decided():
            if len(scanning) == 2:
                return True
            return len(scanning) == 1 and any(not t.is_alive() for t in threads)

        deadline = time.monotonic() + 5
        while not decided() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(decided(), "neither searcher reached the gate")

        try:
            self.assertEqual(len(scanning), 1,
                             "both searchers walked the list at once")
            self.assertTrue(config.search_inprogress,
                            "the refused searcher cleared the flag the "
                            "running one still relies on")
            self.assertEqual(self.replies().count("Another search"), 1)
        finally:
            release_scan.set()
            for thread in threads:
                thread.join(5)

        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertFalse(config.search_inprogress,
                         "the searcher that took the flag did not release it")
        self.assertEqual(self.replies().count("Song.flac"), 1)


class ARefusedSearcherLeavesTheFlagAlone(SearchCase):

    def test_the_flag_is_still_set_after_the_refusal(self):
        """Control: the refusal returns before the try/finally, so it cannot
        release a lock somebody else holds."""
        self.set_config(search_inprogress=True)

        list_mod.execute_search(RecordingSocket(), "carol", "song", "#chan")

        self.assertTrue(config.search_inprogress)
        self.assertIn("Another search", self.replies())


class _SyncThread:
    """threading.Thread that runs its target inline (see tests/test_commands.py)."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


class AnUpdateOnlyClearsTheSearchFlagItRaised(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.debug = silence_debug(announce)
        real_thread_cls = threading.Thread
        threading.Thread = _SyncThread
        self.addCleanup(setattr, threading, "Thread", real_thread_cls)
        real_sleep = time.sleep
        time.sleep = lambda *_a, **_k: None
        self.addCleanup(setattr, time, "sleep", real_sleep)

        import types

        def runner(argv, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout="List of 1 Files\n",
                                         stderr="")

        real = commands.run_watching_for_a_stall
        commands.run_watching_for_a_stall = runner
        self.addCleanup(setattr, commands, "run_watching_for_a_stall", real)

    def test_with_the_pause_off_a_running_search_keeps_its_flag(self):
        """PAUSE_ON_UPDATE False: the rebuild never raised search_inprogress,
        so the flag it finds set belongs to a search still walking the list.
        Clearing it let a second search in on top of the first."""
        self.set_config(PAUSE_ON_UPDATE=False, search_inprogress=True,
                        update_inprogress=False)

        commands.handle_list_update_request("admin", "#chan", authorised=True)

        self.assertFalse(config.update_inprogress, "the rebuild did not end")
        self.assertTrue(config.search_inprogress,
                        "the rebuild cleared a search flag it never raised")

    def test_with_the_pause_on_it_still_releases_what_it_took(self):
        """Control: the flag raised by the rebuild is put back when it ends."""
        self.set_config(PAUSE_ON_UPDATE=True, search_inprogress=False,
                        update_inprogress=False)

        commands.handle_list_update_request("admin", "#chan", authorised=True)

        self.assertFalse(config.update_inprogress)
        self.assertFalse(config.search_inprogress)


def _function(module_name, function_name):
    with io.open(os.path.join(REPO_ROOT, module_name), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return node
    raise AssertionError(f"{module_name} has no {function_name}()")


def _gate_blocks(function):
    """Every `with runtime.list_update_gate:` block in the function."""
    blocks = []
    for node in ast.walk(function):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            expr = item.context_expr
            if (isinstance(expr, ast.Attribute) and expr.attr == "list_update_gate"
                    and isinstance(expr.value, ast.Name) and expr.value.id == "runtime"):
                blocks.append(node)
    return blocks


def _sets_search_inprogress(node):
    return (isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "search_inprogress"
                    for t in node.targets))


def _reads_search_inprogress(node):
    """`getattr(config, 'search_inprogress', ...)` as a condition."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "getattr" and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "search_inprogress")


class TheCheckAndTheSetShareOneLock(unittest.TestCase):
    """Read from the AST, not grepped: the statement has to sit inside the
    with-block, and a token count cannot tell inside from beside."""

    def assert_check_and_set_are_gated(self, module_name, function_name):
        function = _function(module_name, function_name)
        gated = _gate_blocks(function)
        self.assertTrue(gated, f"{function_name}() never takes runtime.list_update_gate")
        inside = [n for block in gated for n in ast.walk(block)]

        self.assertTrue(any(_reads_search_inprogress(n) for n in inside),
                        "the search_inprogress check is outside the gate")
        self.assertTrue(any(_sets_search_inprogress(n) for n in inside),
                        "config.search_inprogress is set outside the gate")

        outside_sets = [n for n in ast.walk(function)
                        if _sets_search_inprogress(n) and n not in inside
                        and isinstance(n.value, ast.Constant) and n.value.value is True]
        self.assertEqual(outside_sets, [],
                         "search_inprogress is still raised somewhere the "
                         "gate does not cover")

    def test_the_searcher(self):
        self.assert_check_and_set_are_gated("list.py", "execute_search")

    def test_the_update(self):
        self.assert_check_and_set_are_gated("commands.py", "handle_list_update_request")

    def test_the_gate_is_the_same_object_on_both_sides(self):
        """One lock, not two that happen to share a name. Both modules bind
        runtime's object, which a !rehash cannot rebind (#235)."""
        import runtime
        self.assertIsInstance(runtime.list_update_gate, type(threading.Lock()))
        for module_name in ("list.py", "commands.py"):
            with io.open(os.path.join(REPO_ROOT, module_name), encoding="utf-8") as handle:
                self.assertNotIn("list_update_gate = threading.Lock()", handle.read())


if __name__ == "__main__":
    unittest.main()
