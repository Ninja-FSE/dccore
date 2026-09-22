"""Orphaned .tmp_*.swap files in data/ after a crash were never swept, and
every state file became owner-only on POSIX (audit L28, #692).

db._atomic_write() creates data/.tmp_XXXX.swap with mkstemp() and swaps it
into place. A hard kill between the two left it behind - and so did a
Ctrl-C, because KeyboardInterrupt is not an Exception and the cleanup
branch did not run - and nothing at startup or on a later write removed
it: every crash added a hidden file to data/. mkstemp() also creates its
file 0600, and the replace carried that through, so hard_bans.txt and
dcc_queue.txt - documented as hand-editable - were unreadable to any other
user after their first save.

db.discard_stale_swaps() removes the leftovers at startup, the way
update_list sweeps its own staging files; the cleanup catches
BaseException; and a new file gets 0644 (the token store 0600, since it
holds secrets) while an existing file keeps the mode it has.
"""

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import platform_compat  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def swaps_in(directory):
    return sorted(n for n in os.listdir(directory) if n.startswith(".tmp_") and n.endswith(".swap"))


class TheSweep(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dccore-swaps-")

    def test_it_removes_the_leftovers_and_nothing_else(self):
        for name in (".tmp_abc.swap", ".tmp_def.swap"):
            with io.open(os.path.join(self.dir, name), "w") as handle:
                handle.write("half a queue")
        for name in ("dcc_queue.txt", ".tmp_other.conf", "stats.txt"):
            with io.open(os.path.join(self.dir, name), "w") as handle:
                handle.write("keep")
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            removed = db.discard_stale_swaps(self.dir)

        self.assertEqual(removed, 2)
        self.assertEqual(swaps_in(self.dir), [])
        self.assertEqual(sorted(os.listdir(self.dir)), [".tmp_other.conf", "dcc_queue.txt", "stats.txt"])
        self.assertIn("Removed 2 leftover temp file(s)", out.getvalue())

    def test_nothing_to_do_says_nothing(self):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(db.discard_stale_swaps(self.dir), 0)
        self.assertEqual(out.getvalue(), "")

    def test_a_missing_directory_is_not_an_error(self):
        self.assertEqual(db.discard_stale_swaps(os.path.join(self.dir, "nope")), 0)

    def test_the_default_is_the_data_directory_the_queue_lives_in(self):
        real = db.DCC_QUEUE_FILE
        db.DCC_QUEUE_FILE = os.path.join(self.dir, "dcc_queue.txt")
        self.addCleanup(setattr, db, "DCC_QUEUE_FILE", real)
        with io.open(os.path.join(self.dir, ".tmp_x.swap"), "w") as handle:
            handle.write("x")
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            db.discard_stale_swaps()

        self.assertEqual(swaps_in(self.dir), [])


class TheWriteCleansUpAfterACtrlC(unittest.TestCase):

    def test_a_keyboard_interrupt_between_mkstemp_and_replace_leaves_nothing(self):
        """The audit's second probe: KeyboardInterrupt is not an Exception."""
        directory = tempfile.mkdtemp(prefix="dccore-ctrlc-")
        real = platform_compat.replace_with_retry

        def interrupted(_src, _dst):
            raise KeyboardInterrupt()
        platform_compat.replace_with_retry = interrupted
        self.addCleanup(setattr, platform_compat, "replace_with_retry", real)

        with self.assertRaises(KeyboardInterrupt):
            db._atomic_write(os.path.join(directory, "dcc_queue.txt"), "{}")

        self.assertEqual(swaps_in(directory), [])

    def test_a_hard_kill_leaves_one_and_the_next_start_sweeps_it(self):
        """The audit's first probe, end to end: a child dies between the two
        steps; the leftover is there; the sweep takes it."""
        directory = tempfile.mkdtemp(prefix="dccore-kill-")
        script = (
            "import os, sys; sys.path.insert(0, %r); import db, platform_compat\n"
            "platform_compat.replace_with_retry = lambda a, b: os._exit(1)\n"
            "db._atomic_write(%r, '{}')\n"
        ) % (REPO_ROOT, os.path.join(directory, "dcc_queue.txt"))
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(swaps_in(directory)), 1, os.listdir(directory))

        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            db.discard_stale_swaps(directory)

        self.assertEqual(swaps_in(directory), [])


@unittest.skipUnless(os.name == "posix", "permission bits are Windows-less")
class TheModeOfAWrittenFile(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dccore-modes-")

    def mode(self, name):
        return stat.S_IMODE(os.stat(os.path.join(self.dir, name)).st_mode)

    def test_a_new_state_file_is_readable_by_others(self):
        db._atomic_write(os.path.join(self.dir, "hard_bans.txt"), "x\n")

        self.assertEqual(self.mode("hard_bans.txt"), 0o644)

    def test_an_existing_file_keeps_the_mode_it_has(self):
        path = os.path.join(self.dir, "dcc_queue.txt")
        with io.open(path, "w") as handle:
            handle.write("{}")
        os.chmod(path, 0o664)

        db._atomic_write(path, "{}")

        self.assertEqual(self.mode("dcc_queue.txt"), 0o664)

    def test_the_token_store_is_owner_only(self):
        real = db.ADMIN_TOKENS_FILE
        db.ADMIN_TOKENS_FILE = os.path.join(self.dir, "adminchat_tokens.json")
        self.addCleanup(setattr, db, "ADMIN_TOKENS_FILE", real)

        db.save_admin_tokens({"x": "y"})

        self.assertEqual(self.mode("adminchat_tokens.json"), 0o600)


class StartupSweeps(unittest.TestCase):

    def test_startup_calls_the_sweep_as_housekeeping(self):
        with io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            body = handle.read()
        start = body.index("def startup(")
        self.assertIn("db.discard_stale_swaps()", body[start:])
        self.assertIn("Could not sweep leftover temp files", body[start:])


if __name__ == "__main__":
    unittest.main()
