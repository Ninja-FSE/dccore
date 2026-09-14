"""Three defects a six-lens audit found before the v1.12 roll.

TWO ARE THE SAME MISTAKE, MADE TWICE

library.folders() takes an optional list name and defaults to the PRIMARY
list's folders. That default is deliberate and correct for the request path:
a request is routed to a list first, and resolving it against that list's
folders is the stronger check - it will not resolve a request against some
other list's library.

Two callers ask the opposite question, though: "is this path one of ours at
all". Both run AFTER routing, when the list name is gone - the pack-time
poison check in dcc.py and the download counter. There, the primary-only
default means every path from every other list reads as foreign.

The pack-time one is destructive. A `!rar` request arriving in a channel bound
to a second list is validated at request time against that list's folders
(resolve_list_folder_with_root is passed wanted_list), accepted, queued, and
packed - and then, at the very end, checked against the PRIMARY's folders,
declared a poisoned queue entry, logged as a security event and deleted.

library.every_folder() is the accessor for that second question.

THE THIRD IS A RACE ON THE IRC READ THREAD

The 353 (NAMES) handler thaws every frozen user who is still in the channel,
and starts a dispatch thread for each. That thread runs check_queue_and_send,
whose freeze sweep deletes EVERY user it finds present in channel_users -
which the 353 handler populated with all of those names two lines earlier.

So the first iteration's thread routinely deletes the keys later iterations
are about to delete with `del`, and the KeyError lands on the IRC read thread.
It is caught by the message loop's `except Exception`, which closes the
socket: a NAMES sync after a reconnect, with two or more frozen users, drops
the link it was sent to recover. The JOIN handler had the same check-then-act
shape against the same sweep.
"""

import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import library  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class EveryFolderAnswersAcrossAllLists(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.music = os.path.join(self.tree.root, "Music")
        self.films = os.path.join(self.tree.root, "Films")
        for path in (self.music, self.films):
            os.makedirs(path, exist_ok=True)
        library.save_lists([
            library.ServedList(name="Music", primary=True, channels=(),
                               folders=(library.Folder("Music", self.music),)),
            library.ServedList(name="Films", primary=False, channels=("#films",),
                               folders=(library.Folder("Films", self.films),)),
        ])

    def test_it_returns_folders_from_every_list(self):
        paths = {entry.path for entry in library.every_folder()}

        self.assertEqual(paths, {self.music, self.films})

    def test_folders_without_a_name_is_still_the_primary_only(self):
        """The default folders() means must NOT change. Every request-path
        caller relies on it, and widening it there would make each of them a
        weaker check than it is today."""
        self.assertEqual([entry.path for entry in library.folders()],
                         [self.music])

    def test_a_shared_folder_appears_once(self):
        """Two lists may legitimately be built from the same directory, and a
        caller iterating this wants each root once."""
        library.save_lists([
            library.ServedList(name="Music", primary=True, channels=(),
                               folders=(library.Folder("Music", self.music),)),
            library.ServedList(name="Films", primary=False, channels=("#films",),
                               folders=(library.Folder("Music", self.music),
                                        library.Folder("Films", self.films),)),
        ])

        paths = [entry.path for entry in library.every_folder()]

        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(set(paths), {self.music, self.films})

    def test_a_single_list_install_sees_exactly_what_it_always_did(self):
        """The ordinary install is one list. every_folder() must not change
        anything for it."""
        library.save_lists([
            library.ServedList(name="Main", primary=True, channels=(),
                               folders=(library.Folder("Music", self.music),)),
        ])

        self.assertEqual([e.path for e in library.every_folder()],
                         [e.path for e in library.folders()])


class APackFromASecondListIsNotDestroyed(DCCoreTestCase):
    """The pack-time check and the request-time check have to agree about
    which paths are ours. When they disagree, the request path admits a row
    and the pack path deletes it as poisoned."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.music = os.path.join(self.tree.root, "Music")
        self.films = os.path.join(self.tree.root, "Films")
        for path in (self.music, self.films):
            os.makedirs(path, exist_ok=True)
        library.save_lists([
            library.ServedList(name="Music", primary=True, channels=(),
                               folders=(library.Folder("Music", self.music),)),
            library.ServedList(name="Films", primary=False, channels=("#films",),
                               folders=(library.Folder("Films", self.films),)),
        ])

    def guard_accepts(self, path):
        """THE REAL FUNCTION, not a copy of it.

        This test first reimplemented the check inline. It passed against the
        broken code: reverting dcc.py to the primary-only version changed
        nothing, because the test was exercising its own copy. That is the
        "guard that reads rather than executes" failure this repository has
        been bitten by three times, and mutation testing is what caught it
        here."""
        import dcc

        return dcc.path_is_in_our_library(path)

    def test_a_folder_from_the_second_list_passes(self):
        album = os.path.join(self.films, "Spider-Noir (2026)", "Season 01")
        os.makedirs(album, exist_ok=True)

        self.assertTrue(
            self.guard_accepts(album),
            "a !rar admitted in a channel bound to the Films list was "
            "destroyed at pack time as a poisoned queue entry")

    def test_a_folder_from_the_primary_still_passes(self):
        album = os.path.join(self.music, "Artist", "Album")
        os.makedirs(album, exist_ok=True)

        self.assertTrue(self.guard_accepts(album))

    def test_a_path_outside_every_list_is_still_refused(self):
        """The guard is a security check. Widening it from one list to all
        lists must not widen it to the whole filesystem."""
        outside = os.path.join(self.tree.root, "NotServed", "secrets")
        os.makedirs(outside, exist_ok=True)

        self.assertFalse(self.guard_accepts(outside))

    def test_the_parent_of_a_served_folder_is_still_refused(self):
        self.assertFalse(self.guard_accepts(self.tree.root))

    def test_a_traversal_out_of_a_served_folder_is_still_refused(self):
        self.assertFalse(
            self.guard_accepts(os.path.join(self.films, "..", "NotServed")))


class TheDownloadCounterKeepsARelativeKey(DCCoreTestCase):
    """A file served from a second list fell through to the absolute-path
    branch: a counter key holding a drive letter, which does not match the
    same file counted anywhere else - and puts a real path in a stats table
    that the dashboard renders."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        # BOTH directories are created, and both names are used exactly as
        # spelled. This setUp originally passed `<root>/Music` for the primary
        # without creating it, and relied on TempTree's own `<root>/music`
        # existing - which is a different path on Linux and the same one on
        # NTFS. It passed on Windows and failed every CI run on ubuntu with
        # "not a folder on this machine", which is the plainest case of the
        # portability trap this project is trying to avoid.
        self.music = os.path.join(self.tree.root, "Music")
        self.films = os.path.join(self.tree.root, "Films")
        for path in (self.music, self.films):
            os.makedirs(path, exist_ok=True)
        library.save_lists([
            library.ServedList(name="Music", primary=True, channels=(),
                               folders=(library.Folder("Music", self.music),)),
            library.ServedList(name="Films", primary=False, channels=("#films",),
                               folders=(library.Folder("Films", self.films),)),
        ])

    def test_a_file_from_the_second_list_is_keyed_relatively(self):
        import dcc

        served = os.path.join(self.films, "Sci-Fi", "Some.Film.2026.mkv")

        key = dcc.library_count_key(served)

        self.assertEqual(key, os.path.join("Films", "Sci-Fi", "Some.Film.2026.mkv"))
        self.assertNotIn(self.tree.root, key)

    def test_a_file_under_no_list_still_keeps_its_absolute_path(self):
        """Unchanged behaviour: a temp archive is not a reason to lose the
        row."""
        import dcc

        stray = os.path.join(self.tree.root, "tmp", "archive.rar")

        self.assertEqual(dcc.library_count_key(stray), stray)


class ThawingTwoFrozenUsersDoesNotDropTheLink(DCCoreTestCase):
    """The concurrent delete is what makes this real: the sweep in
    check_queue_and_send removes exactly the users the 353 handler is
    iterating."""

    def test_it_thaws_the_users_who_are_frozen(self):
        import irc

        config.frozen_queues = {"alice": 1.0, "bob": 2.0}

        self.assertEqual(irc.thaw_frozen_users(["alice", "bob", "carol"]),
                         ["alice", "bob"])

    def test_a_thawed_user_is_actually_removed(self):
        """The list it returns must mean the freeze is gone. A version that
        reported a thaw without removing the key would leave the queue frozen
        and the five-minute delete timer running."""
        import irc

        config.frozen_queues = {"alice": 1.0}

        irc.thaw_frozen_users(["alice"])

        self.assertNotIn("alice", config.frozen_queues)

    def test_losing_the_race_is_not_an_error_and_is_not_claimed(self):
        """The sweep on the thread started for the first user removes the
        second user's key before the loop reaches him. That must neither raise
        nor be reported as a thaw this call performed."""
        import irc

        config.frozen_queues = {"alice": 1.0, "bob": 2.0}
        config.frozen_queues.pop("bob", None)

        self.assertEqual(irc.thaw_frozen_users(["alice", "bob"]), ["alice"])

    def test_one_user_thaw_reports_who_won(self):
        import irc

        config.frozen_queues = {"alice": 1.0}

        self.assertTrue(irc.thaw_one_user("alice"))
        self.assertFalse(irc.thaw_one_user("alice"))

    def test_a_real_concurrent_sweep_does_not_raise(self):
        """Threads, not a simulated interleaving, and driving the REAL
        function."""
        import irc

        config.frozen_queues = {f"user{i}": 1.0 for i in range(200)}
        errors = []

        def sweeper():
            for name in list(config.frozen_queues):
                config.frozen_queues.pop(name, None)

        def thawer():
            try:
                irc.thaw_frozen_users(list(config.frozen_queues))
            except Exception as err:  # noqa: BLE001 - the defect is any raise
                errors.append(err)

        threads = [threading.Thread(target=sweeper), threading.Thread(target=thawer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])

    def test_the_check_and_the_removal_cannot_be_two_steps(self):
        """The race, made DETERMINISTIC.

        A thread-based test cannot reliably hit the window between an `in`
        test and a `del` - it is a few bytecodes wide, and the threaded test
        above passed against the check-then-act version. So this hands the
        function a dict whose membership test removes the key, which is
        exactly what the freeze sweep does between those two statements.

        `pop()` never asks `__contains__` at all, so it removes the key in
        one step and reports the thaw. `if key in frozen: del frozen[key]`
        asks, is told yes, and then raises KeyError on the `del` - on the IRC
        READ THREAD, where the message loop answers it by closing the socket.
        """
        import irc

        class SweptDuringTheCheck(dict):
            def __contains__(self, key):
                present = super().__contains__(key)
                if present:
                    super().pop(key, None)  # the sweep, winning the race
                return present

        config.frozen_queues = SweptDuringTheCheck({"alice": 1.0})

        # A KeyError here IS the defect - it reaches the read loop as a
        # dropped connection.
        self.assertTrue(irc.thaw_one_user("alice"))
        self.assertNotIn("alice", dict(config.frozen_queues))

    def test_the_batch_thaw_survives_the_same_interleaving(self):
        import irc

        class SweptDuringTheCheck(dict):
            def __contains__(self, key):
                present = super().__contains__(key)
                if present:
                    super().pop(key, None)
                return present

        config.frozen_queues = SweptDuringTheCheck({"alice": 1.0, "bob": 2.0})

        self.assertEqual(irc.thaw_frozen_users(["alice", "bob"]),
                         ["alice", "bob"])

    def test_a_missing_frozen_queues_is_not_a_crash(self):
        """config is reloaded by !rehash; a caller must not assume the
        attribute survived."""
        import irc

        previous = getattr(config, "frozen_queues", None)
        try:
            delattr(config, "frozen_queues")
            self.assertFalse(irc.thaw_one_user("alice"))
            self.assertEqual(irc.thaw_frozen_users(["alice"]), [])
        finally:
            if previous is not None:
                config.frozen_queues = previous


if __name__ == "__main__":
    unittest.main()
