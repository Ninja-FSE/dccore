"""#580: one short line could cost a full-library scan, on its own thread, ten times over.

A name that is not in the first folder's root makes the request handler stream
every published list (and, until #1376, then os.walk() every configured folder). Each request
runs on its own daemon thread and the flood gate allows a nick ten requests per
five seconds, with no penalty once a mute lapses - so a nick pasting a stale
row (or an attacker inventing names) starts ten concurrent metadata scans of a
multi-terabyte library, and on spinning disks they outlast the cycle and pile up.

Two bounds: a name that just missed is answered from memory for a minute, and
only a few scans run at once (the rest are told the bot is busy, at once).
"""

import os
import sys
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import dcc  # noqa: E402

from tests.test_path_security import PathSecurityBase, quiet  # noqa: E402


class LookupBase(PathSecurityBase):
    """A bot whose published list names the tree's tracks. A scan is one
    read of the published lists: since #1376 that is the whole lookup, and a
    name no list places is not found (it used to be the os.walk() after it
    that these counted, and that found the tracks)."""

    def setUp(self):
        super().setUp()
        dcc.forget_library_lookups()           # misses, hits and folders (#886)
        self.addCleanup(dcc.forget_library_lookups)
        self.published = os.path.join(self.tree.lists, "published-list.txt")
        self.publish(os.path.basename(path) for path in self.tree.tracks)
        paths = mock.patch.object(dcc.list_mod, "all_list_paths",
                                  lambda name=None: [self.published])
        paths.start()
        self.addCleanup(paths.stop)
        self.scans = []
        real_lookup = dcc.path_from_published_lists

        def counting_lookup(list_paths, *a, **k):
            self.scans.append(list(list_paths))
            return real_lookup(list_paths, *a, **k)

        patch = mock.patch.object(dcc, "path_from_published_lists", counting_lookup)
        patch.start()
        self.addCleanup(patch.stop)

    def publish(self, names):
        """The published list: the tree's album as its one folder, `names` in it."""
        with open(self.published, "w", encoding="utf-8") as handle:
            handle.write(dcc.list_mod.LIST_FOLDER_PREFIX + "Metallica\\Black Album (1991)\\\n")
            for name in names:
                handle.write(f"!{config.NICKNAME} {name}  ::INFO:: 4.00KB\n")

    def ask(self, name, user="dave"):
        with quiet():
            dcc.handle_download_request(self.sock, user, name, "#dccore-test")

    def errors(self):
        return [args[1] for kind, args in self.notices if kind == "error"]


class AMissIsRememberedForAMinute(LookupBase):

    def test_the_same_missing_name_scans_once_however_often_it_is_asked(self):
        for _ in range(10):
            self.ask("Stale Row - Nothing Here.flac")

        self.assertEqual(len(self.scans), 1, self.scans)
        self.assertEqual(self.errors(), ["file_not_found"] * 10)

    def test_the_memory_is_shared_between_users_and_case(self):
        self.ask("Nothing Here.flac", user="dave")
        self.ask("NOTHING here.FLAC", user="erin")
        self.assertEqual(len(self.scans), 1)

    def test_a_different_missing_name_is_a_new_lookup(self):
        self.ask("One.flac")
        self.ask("Two.flac")
        self.assertEqual(len(self.scans), 2)

    def test_the_memory_expires(self):
        self.ask("Nothing Here.flac")
        for key in list(dcc._lookup_misses):
            dcc._lookup_misses[key] -= dcc.LOOKUP_MISS_TTL_SECONDS + 1
        self.ask("Nothing Here.flac")
        self.assertEqual(len(self.scans), 2)

    def test_the_memory_is_bounded(self):
        with mock.patch.object(dcc, "LOOKUP_MISS_MEMORY", 5):
            for number in range(12):
                dcc._note_lookup_miss(("main", f"name {number}"))
        self.assertEqual(len(dcc._lookup_misses), 5)
        self.assertIn(("main", "name 11"), dcc._lookup_misses, "the newest survive")
        self.assertNotIn(("main", "name 0"), dcc._lookup_misses)

    def test_a_file_that_exists_is_never_remembered_as_missing(self):
        self.ask(os.path.basename(self.tree.tracks[0]))
        self.assertEqual(dcc._lookup_misses, {})
        self.assertNotIn("file_not_found", self.errors())

    def test_a_file_added_after_a_miss_is_not_found_until_it_is_listed(self):
        """Not found while the list does not name it, memory or not (#1376):
        it is requestable after the next rebuild."""
        name = "Brand New Song.flac"
        self.ask(name)
        with open(os.path.join(self.tree.album, name), "wb") as handle:
            handle.write(b"\x00" * 1024)

        def forget_the_miss():
            for key in list(dcc._lookup_misses):
                dcc._lookup_misses[key] -= dcc.LOOKUP_MISS_TTL_SECONDS + 1
        forget_the_miss()
        self.ask(name)
        self.assertEqual(self.errors(), ["file_not_found"] * 2, "on disk, but in no list")

        self.publish([os.path.basename(path) for path in self.tree.tracks] + [name])
        forget_the_miss()
        self.ask(name)
        self.assertEqual(self.errors(), ["file_not_found"] * 2, "listed by the rebuild, then found")


class OnlyAFewScansRunAtOnce(LookupBase):
    """Since #886 a request WAITS LOOKUP_SCAN_WAIT_SECONDS for a slot before
    being refused, so these shorten the wait rather than sit through it. The
    bound itself - only MAX_CONCURRENT_LIBRARY_SCANS scans at a time, and the
    rest told the bot is busy without touching the disk - is unchanged."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(dcc, "LOOKUP_SCAN_WAIT_SECONDS", 0.05)
        patch.start()
        self.addCleanup(patch.stop)
        dcc.forget_library_lookups()
        self.addCleanup(dcc.forget_library_lookups)

    def hold_every_slot(self):
        held = 0
        while dcc._library_scans.acquire(blocking=False):
            held += 1
        self.addCleanup(lambda: [dcc._library_scans.release() for _ in range(held)])
        self.assertEqual(held, dcc.MAX_CONCURRENT_LIBRARY_SCANS)

    def test_a_request_beyond_the_limit_is_told_the_bot_is_busy_and_scans_nothing(self):
        """Once the wait has run out, that is."""
        self.hold_every_slot()
        self.ask("Something Else.flac")
        self.assertEqual(self.errors(), ["busy"])
        self.assertEqual(self.scans, [])

    def test_being_busy_is_not_remembered_as_a_miss(self):
        self.hold_every_slot()
        self.ask("Something Else.flac")
        self.assertEqual(dcc._lookup_misses, {})

    def test_the_slot_is_given_back_after_a_miss(self):
        self.ask("Nothing Here.flac")
        self.assertTrue(dcc._library_scans.acquire(blocking=False))
        dcc._library_scans.release()
        for _ in range(dcc.MAX_CONCURRENT_LIBRARY_SCANS):
            self.assertTrue(dcc._library_scans.acquire(blocking=False))
        for _ in range(dcc.MAX_CONCURRENT_LIBRARY_SCANS):
            dcc._library_scans.release()

    def test_the_slot_is_given_back_when_the_scan_blows_up(self):
        with mock.patch.object(dcc.os, "walk", side_effect=OSError("disk gone")):
            try:
                self.ask("Nothing Here.flac")
            except Exception:
                pass
        for _ in range(dcc.MAX_CONCURRENT_LIBRARY_SCANS):
            self.assertTrue(dcc._library_scans.acquire(blocking=False))
        for _ in range(dcc.MAX_CONCURRENT_LIBRARY_SCANS):
            dcc._library_scans.release()

    def test_a_file_in_the_first_folder_needs_no_scan_and_no_slot(self):
        top_level = os.path.join(self.tree.music, "Root Song.flac")
        with open(top_level, "wb") as handle:
            handle.write(b"\x00" * 1024)
        self.hold_every_slot()
        self.ask("Root Song.flac")
        self.assertNotIn("busy", self.errors())
        self.assertEqual(self.scans, [])


class Wiring(unittest.TestCase):
    def test_the_busy_message_exists_and_says_to_try_again(self):
        import inspect
        import announce
        source = inspect.getsource(announce.send_dcc_error)
        self.assertIn('"busy": "Error: Busy looking up other files - try again in a moment."', source)


if __name__ == "__main__":
    unittest.main()
