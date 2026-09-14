"""The two high-severity findings of the full-program audit.

Unrelated subsystems, one shape: the daemon silently does the wrong thing and
says it succeeded.

1. A SERVED FOLDER THAT IS A DRIVE ROOT REFUSED EVERY FILE UNDER IT.

   dcc.is_safe_path() and library.is_inside() both compared with

       path.startswith(base + os.sep)

   and os.path.realpath("D:\\") is "D:\\" - it already ends in a separator, so
   that built "D:\\\\", a doubled separator no real path can start with. Every
   file on a drive served whole was refused.

   Refused, note, not admitted: the direction is safe, which is exactly why it
   could sit there. The master list advertised every file on the drive, every
   request for one came back as a path violation, and the refusal was logged
   as a security event rather than the configuration problem it was. Reachable
   the moment #164 step 4 shipped, because the folder editor accepts a drive
   root with a 200.

2. TWO OVERLAPPING REHASHES MADE ONE OF THEM PART EVERY CHANNEL.

   handle_rehash_request() reloads the modules, then compares the channel list
   it reads afterwards against the one it read before to decide what to JOIN
   and what to PART. A second rehash's reload puts config.CHANNEL back to its
   literal None for about a millisecond, and a first rehash reading its "new"
   list inside that window saw NO CHANNELS - so every channel the bot was in
   fell into the PART branch.

   The audit reproduced it with nothing patched, 4 times in 60 overlapping
   runs: PART for every channel including the debug channel, no JOIN, no
   NAMES, channel_users emptied, and "[REHASH SYNC] Channel sync completed
   successfully." in the log. dcc.py treats channel_users as proof a user is
   present, so every queue then froze.

   Easy to reach: webserver.py fires a rehash on EVERY Settings save and every
   password change, and irc.py and adminchat.py each spawn an unguarded thread
   per "!rehash".
"""

import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import commands  # noqa: E402
import dcc  # noqa: E402
import library  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class AFolderThatIsARootStillContainsItsFiles(DCCoreTestCase):
    """Both functions, because the validator and the gate disagreeing is its
    own bug: library.is_inside() decides whether a folder set may be SAVED and
    dcc.is_safe_path() decides whether a file may be SENT."""

    def roots(self):
        """Whatever this machine's filesystem calls a root."""
        if os.name == "nt":
            drive = os.path.splitdrive(os.path.abspath(REPO_ROOT))[0]
            return [drive + os.sep]
        return ["/"]

    def test_a_root_contains_a_path_under_it(self):
        for root in self.roots():
            inside = os.path.join(root, os.listdir(root)[0])
            with self.subTest(root=root):
                self.assertTrue(dcc.is_safe_path(root, inside))
                self.assertTrue(library.is_inside(root, inside))

    def test_a_root_contains_itself(self):
        for root in self.roots():
            with self.subTest(root=root):
                self.assertTrue(dcc.is_safe_path(root, root))
                self.assertTrue(library.is_inside(root, root))

    def test_a_root_does_not_contain_a_path_on_another_root(self):
        """The gate must still refuse. On POSIX there is only one root, so
        this is a Windows-shaped check and skips where it is meaningless."""
        if os.name != "nt":
            self.skipTest("one root on this platform")
        drive = os.path.splitdrive(os.path.abspath(REPO_ROOT))[0]
        other = "Z:" if drive.upper() != "Z:" else "Y:"

        self.assertFalse(dcc.is_safe_path(drive + os.sep,
                                          other + os.sep + "somewhere"))

    def test_the_sibling_prefix_trap_is_still_refused(self):
        """The property the separator boundary exists for, unchanged by the
        fix: "/srv/library-backup" begins with "/srv/library" as a string and
        is not inside it."""
        tree = self.make_tree().root
        base = os.path.join(tree, "library")
        sibling = os.path.join(tree, "library-backup", "track.flac")
        os.makedirs(base, exist_ok=True)
        os.makedirs(os.path.dirname(sibling), exist_ok=True)

        self.assertFalse(dcc.is_safe_path(base, sibling))
        self.assertFalse(library.is_inside(base, sibling))

    def test_an_ordinary_folder_is_unaffected(self):
        """Control. The fix must only change the root case."""
        tree = self.make_tree().root
        base = os.path.join(tree, "library")
        inside = os.path.join(base, "Artist", "track.flac")
        os.makedirs(os.path.dirname(inside), exist_ok=True)

        self.assertTrue(dcc.is_safe_path(base, inside))
        self.assertTrue(library.is_inside(base, inside))

    def test_a_base_written_with_a_trailing_separator(self):
        """An operator typing "D:\\Music\\" into the folder editor is the same
        folder as "D:\\Music", and was already handled - this keeps it so."""
        tree = self.make_tree().root
        base = os.path.join(tree, "library")
        inside = os.path.join(base, "track.flac")
        os.makedirs(base, exist_ok=True)

        self.assertTrue(dcc.is_safe_path(base + os.sep, inside))
        self.assertTrue(library.is_inside(base + os.sep, inside))


class OnlyOneRehashRunsAtATime(unittest.TestCase):

    def setUp(self):
        self.original = commands._handle_rehash_request
        self.addCleanup(setattr, commands, "_handle_rehash_request",
                        self.original)

    def test_concurrent_rehashes_never_overlap(self):
        """The body is stood in for, because what is under test is the
        serialisation and not the daemon's own reload behaviour."""
        overlaps = []
        depth = [0]
        gate = threading.Lock()

        def body(_user, _target):
            with gate:
                depth[0] += 1
                if depth[0] > 1:
                    overlaps.append(True)
            time.sleep(0.005)
            with gate:
                depth[0] -= 1

        commands._handle_rehash_request = body
        threads = [threading.Thread(target=commands.handle_rehash_request,
                                    args=(f"caller{i}", "#chan"),
                                    kwargs={"authorised": True})
                   for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)

        self.assertEqual(overlaps, [])

    def test_every_rehash_still_runs(self):
        """WAITS, rather than dropping the second one. A dashboard save writes
        settings.conf and THEN triggers a rehash, and the one already running
        may have read the file before that write landed - so a dropped second
        rehash silently loses the operator's change."""
        ran = []
        commands._handle_rehash_request = lambda user, _t: ran.append(user)

        threads = [threading.Thread(target=commands.handle_rehash_request,
                                    args=(f"caller{i}", "#chan"),
                                    kwargs={"authorised": True})
                   for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)

        self.assertEqual(len(ran), 6)

    def test_the_lock_is_released_when_the_body_raises(self):
        """Otherwise one failed rehash wedges every rehash after it, which is
        worse than the bug this lock exists for."""
        def boom(_user, _target):
            raise RuntimeError("bang")

        commands._handle_rehash_request = boom

        with self.assertRaises(RuntimeError):
            commands.handle_rehash_request("someone", "#chan", authorised=True)

        self.assertFalse(runtime.rehash_lock.locked())

    def test_an_unauthorised_caller_never_takes_the_lock(self):
        """The auth check runs first. A rejected rehash that still queued
        behind a running one would be a way to stall the daemon from a
        channel."""
        commands._handle_rehash_request = lambda *_a: None
        runtime.rehash_lock.acquire()
        try:
            commands.handle_rehash_request("nobody", "#chan", authorised=False)
        finally:
            runtime.rehash_lock.release()

    def test_the_lock_lives_in_runtime(self):
        """commands.py is one of the modules a rehash RELOADS, so a lock
        allocated there would be a new lock every time - the founding reason
        runtime.py exists."""
        import io

        with io.open(os.path.join(REPO_ROOT, "commands.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("runtime.rehash_lock", source)
        self.assertNotIn("rehash_lock = threading.Lock()", source)


if __name__ == "__main__":
    unittest.main()
