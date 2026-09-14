"""#432: an unlocked queue scan could abort the whole advert cycle.

dcc.get_total_queued_count() iterated `config.dcc_queue.items()` directly.
Every writer that adds or removes a key does so under `queue_lock`
(dcc.py's own request/transfer path, commands.py, db.py) - but this reader
took no lock at all, so a key added or removed at the exact microsecond this
loop was mid-scan raised "dictionary changed size during iteration".

That escaped all the way up through announce_worker()'s per-channel loop,
past the `time.sleep(config.ANNOUNCE_INTERVAL)` below it, into the outer
`except Exception as loop_error:` - which restarts the whole cycle from the
first channel after a 10s sleep. Channels after the failure point missed
that cycle's advert entirely; channels before it got a duplicate 10s early.

Two halves, two kinds of test:

  * get_total_queued_count() is an ordinary callable, so its half is a real
    concurrent reproduction - a mutator thread hammering config.dcc_queue
    while a reader thread calls the real function, exactly as the audit's
    own repro did.

  * announce_worker() is one of only two functions no test in this suite
    calls directly - see test_announce_target_is_one_channel.py's own
    comment: it is a `while True:` loop with no clean exit, so its body is
    read out of the source/AST instead, the same way that file already does
    for the same function.
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

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheQueueTotalSurvivesAConcurrentMutation(DCCoreTestCase):
    """The exact race the audit measured: a writer adding/removing keys under
    queue_lock while a lockless reader is mid-scan."""

    def setUp(self):
        super().setUp()
        config.dcc_queue = {f"user{i}": [{"file": "x"}] for i in range(300)}

    def test_a_concurrent_add_and_remove_never_raises(self):
        stop = threading.Event()
        errors = []

        def mutate():
            i = 0
            while not stop.is_set():
                key = f"mut{i % 50}"
                with dcc.queue_lock:
                    config.dcc_queue[key] = [{"file": "x"}]
                with dcc.queue_lock:
                    config.dcc_queue.pop(key, None)
                i += 1

        def read():
            deadline = time.time() + 0.5
            while time.time() < deadline:
                try:
                    dcc.get_total_queued_count()
                except RuntimeError as err:
                    errors.append(err)
                    return

        mutator = threading.Thread(target=mutate, daemon=True)
        mutator.start()
        try:
            read()
        finally:
            stop.set()
            mutator.join(2)

        self.assertEqual(errors, [],
                         "get_total_queued_count() must survive a concurrent "
                         "mutation of config.dcc_queue without a lock of its "
                         "own - it snapshots the values instead")

    def test_the_count_is_still_correct_with_no_concurrent_writer(self):
        """The fix must not change the answer, only how it is computed."""
        config.dcc_queue = {
            "alice": [{"file": "a"}, {"file": "b"}],
            "bob": [{"file": "c"}],
        }

        self.assertEqual(dcc.get_total_queued_count(), 3)

    def test_it_does_not_take_queue_lock_itself(self):
        """dcc.py's own request-path callers (start_dcc_send() and its
        pack-request sibling) call this function while ALREADY holding
        queue_lock, which is a plain threading.Lock - not reentrant. Taking
        it here too would deadlock every file and pack request the moment
        that path is exercised."""
        acquired = dcc.queue_lock.acquire(timeout=0)
        try:
            self.assertTrue(acquired, "queue_lock was already held; the test "
                                       "setup is wrong")
            # If get_total_queued_count() tried to acquire queue_lock itself,
            # this call would hang forever with the lock held on this same
            # thread. Bounded by the thread itself: an actual deadlock here
            # would hang the whole test process, which is the point - there
            # is no timeout to assert against, only a call that must return.
            total = dcc.get_total_queued_count()
        finally:
            if acquired:
                dcc.queue_lock.release()

        self.assertIsInstance(total, int)


class TheAdvertLoopIsolatesEachChannel(unittest.TestCase):
    """announce_worker() is one of only two functions no test in this suite
    calls directly (see test_announce_target_is_one_channel.py's
    TheAdvertLoopReadsTheChannelsSafely) - a `while True:` loop with no clean
    exit. Read the structure out of the AST instead, the same way #467's PING
    literal was verified out of the AST rather than the source text.
    """

    def _worker_function(self):
        with io.open(os.path.join(REPO_ROOT, "announce.py"),
                     encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "announce_worker":
                return node
        self.fail("announce_worker() not found in announce.py")

    def _channel_for_loop(self, func_node):
        for node in ast.walk(func_node):
            if (isinstance(node, ast.For)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "chan"):
                return node
        self.fail("no 'for chan in ...:' loop found inside announce_worker()")

    def _calls_dotted(self, node, dotted):
        """True if `node` contains a Call whose attribute access spells `dotted`
        (e.g. "dcc.get_total_queued_count")."""
        target = dotted.split(".")
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                names = []
                cur = sub.func
                while isinstance(cur, ast.Attribute):
                    names.insert(0, cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    names.insert(0, cur.id)
                if names == target:
                    return True
        return False

    def test_the_risky_body_is_wrapped_in_a_try(self):
        loop = self._channel_for_loop(self._worker_function())
        tries = [n for n in loop.body if isinstance(n, ast.Try)]

        self.assertTrue(tries,
                        "the per-channel loop body has no try/except - one "
                        "channel's exception (e.g. from "
                        "dcc.get_total_queued_count()) will escape the "
                        "whole for-loop and abort the advert cycle")

    def test_the_risky_call_is_actually_inside_the_try(self):
        """A try/except that wraps something else while the real risky call
        sits outside it would pass the test above for the wrong reason."""
        loop = self._channel_for_loop(self._worker_function())
        tries = [n for n in loop.body if isinstance(n, ast.Try)]
        self.assertTrue(tries)

        covered = any(self._calls_dotted(t, "dcc.get_total_queued_count")
                      for t in tries)
        self.assertTrue(covered,
                        "dcc.get_total_queued_count() is not inside the "
                        "per-channel try/except")

    def test_the_exception_handler_does_not_re_raise(self):
        loop = self._channel_for_loop(self._worker_function())
        tries = [n for n in loop.body if isinstance(n, ast.Try)]
        self.assertTrue(tries)

        for try_node in tries:
            self.assertTrue(try_node.handlers,
                            "a try with no except handlers propagates just "
                            "like no try at all")
            for handler in try_node.handlers:
                for stmt in ast.walk(handler):
                    self.assertNotIsInstance(
                        stmt, ast.Raise,
                        "the per-channel handler re-raises - that puts the "
                        "exception right back where it was, still able to "
                        "abort the whole cycle")

    def test_the_interval_sleep_is_reachable_regardless_of_a_channel_failure(self):
        """The other half of the bug: even a caught exception must not skip
        time.sleep(config.ANNOUNCE_INTERVAL) - it has to sit textually after
        the whole for-loop, not inside a branch the try could jump past."""
        func = self._worker_function()
        loop = self._channel_for_loop(func)

        # Find the statement list that directly contains the for-loop (its
        # parent block), then the statement immediately following it there.
        parent_body = None
        for node in ast.walk(func):
            body = getattr(node, "body", None)
            if isinstance(body, list) and loop in body:
                parent_body = body
                break
        self.assertIsNotNone(parent_body, "could not locate the for-loop's "
                                          "enclosing block")

        index = parent_body.index(loop)
        self.assertLess(index + 1, len(parent_body),
                        "nothing follows the channel loop at all")
        following = parent_body[index + 1]
        self.assertTrue(
            self._calls_dotted(following, "time.sleep"),
            "the statement right after the channel loop is not the "
            "ANNOUNCE_INTERVAL sleep - a per-channel failure must not be "
            "able to skip past it")


if __name__ == "__main__":
    unittest.main()
