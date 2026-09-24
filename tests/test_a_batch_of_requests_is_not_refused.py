"""Pasting several rows from the list is served, not refused (#886).

Reported live: a user pasted nine request lines in about seven seconds -
the ordinary way these lists are used - and was answered "Error: Busy
looking up other files - try again in a moment", which is also the one
thing that risks the flood gate.

Every row in a list is a bare filename while the files themselves live in
subfolders, so *every* request took #580's library scan; only misses were
remembered, so nothing a scan learned was ever reused; and the two scan
slots were taken with a non-blocking acquire, so the rest of a batch
bounced at once.

Three memories now, each verified before it is trusted: the name a scan
resolved, the folders recent lookups landed in (a batch is nearly always
siblings in one album), and a short wait for a slot. What may be sent is
unchanged - is_safe_path() still checks every resolved path against every
configured root.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402

from tests.test_an_unknown_filename_does_not_scan_the_library_unbounded import LookupBase  # noqa: E402


class Remembering(LookupBase):

    def setUp(self):
        super().setUp()
        dcc.forget_library_lookups()
        self.addCleanup(dcc.forget_library_lookups)

    def a_track(self, index=0):
        return os.path.basename(self.tree.tracks[index])

    def test_the_same_file_asked_for_twice_is_scanned_once(self):
        self.ask(self.a_track())
        scans_after_first = len(self.walks)
        self.assertEqual(scans_after_first, 1, "the first request should have scanned")
        self.ask(self.a_track())

        self.assertEqual(len(self.walks), scans_after_first,
                         "the second request paid for the scan again")
        self.assertEqual(self.errors(), [])

    def test_a_sibling_in_the_same_folder_costs_no_scan(self):
        """The reported case: the rest of a pasted batch are siblings."""
        first, second = self.a_track(0), self.a_track(1)
        self.assertNotEqual(first, second)

        self.ask(first)
        scans_after_first = len(self.walks)
        self.assertEqual(scans_after_first, 1, "the first request should have scanned")
        self.ask(second)

        self.assertEqual(len(self.walks), scans_after_first, self.walks)
        self.assertEqual(self.errors(), [])

    def test_a_whole_pasted_batch_is_served(self):
        names = [os.path.basename(path) for path in self.tree.tracks]
        self.assertGreaterEqual(len(names), 2)

        for name in names:
            self.ask(name)

        self.assertEqual(self.errors(), [], "a row in the batch was refused")
        self.assertEqual(len(self.walks), 1,
                         "one scan for the whole batch, not one per row")

    def test_a_file_that_has_gone_is_not_served_from_memory(self):
        name = self.a_track()
        self.ask(name)
        os.remove(self.tree.tracks[0])

        self.ask(name)

        self.assertIn("file_not_found", self.errors())

    def test_the_memory_expires(self):
        name = self.a_track()
        self.ask(name)
        scans_after_first = len(self.walks)
        self.assertEqual(scans_after_first, 1, "the first request should have scanned")
        with mock.patch.object(dcc, "LOOKUP_HIT_TTL_SECONDS", -1), \
                mock.patch.object(dcc, "LOOKUP_FOLDER_MEMORY", 0):
            dcc._lookup_folders.clear()
            self.ask(name)

        self.assertGreater(len(self.walks), scans_after_first)

    def test_the_memories_are_bounded(self):
        with mock.patch.object(dcc, "LOOKUP_HIT_MEMORY", 5), \
                mock.patch.object(dcc, "LOOKUP_FOLDER_MEMORY", 3):
            for number in range(12):
                dcc._note_lookup_hit(("main", "name %d" % number),
                                     os.path.join(self.tree.music, "f%d" % number, "x.flac"))

        self.assertEqual(len(dcc._lookup_hits), 5)
        self.assertIn(("main", "name 11"), dcc._lookup_hits, "the newest survive")
        self.assertEqual(len(dcc._lookup_folders["main"]), 3)

    def test_a_missing_name_is_still_remembered_as_missing(self):
        """The #580 bound is untouched by any of this."""
        for _ in range(4):
            self.ask("Nothing Here At All.flac")

        self.assertEqual(self.errors(), ["file_not_found"] * 4)
        self.assertEqual(len(self.walks), 1)


class WaitingForASlot(LookupBase):

    def setUp(self):
        super().setUp()
        dcc.forget_library_lookups()
        self.addCleanup(dcc.forget_library_lookups)

    def test_the_request_waits_for_a_slot_rather_than_refusing_at_once(self):
        """The change itself: a deadline, not blocking=False. A slot that
        frees up inside it is used, and the user is served."""
        asked = {}

        class SlotThatFreesUp:
            def acquire(self, blocking=True, timeout=None):
                asked["blocking"] = blocking
                asked["timeout"] = timeout
                return True                     # as if a scan had just finished

            def release(self):
                asked["released"] = True

        with mock.patch.object(dcc, "_library_scans", SlotThatFreesUp()):
            self.ask(os.path.basename(self.tree.tracks[0]))

        self.assertEqual(asked.get("timeout"), dcc.LOOKUP_SCAN_WAIT_SECONDS,
                         "the request bounced instead of waiting for a slot")
        self.assertNotEqual(asked.get("blocking"), False)
        self.assertTrue(asked.get("released"), "the slot was not given back")
        self.assertEqual(self.errors(), [])

    def test_it_still_says_busy_when_the_wait_itself_runs_out(self):
        held = 0
        while dcc._library_scans.acquire(blocking=False):
            held += 1
        self.addCleanup(lambda: [dcc._library_scans.release() for _ in range(held)])

        with mock.patch.object(dcc, "LOOKUP_SCAN_WAIT_SECONDS", 0.05):
            self.ask("Something Else Entirely.flac")

        self.assertEqual(self.errors(), ["busy"])
        self.assertEqual(self.walks, [])


class TheMemoryIsAHintNotAnAuthority(unittest.TestCase):

    def test_a_remembered_path_that_no_longer_exists_is_dropped(self):
        dcc.forget_library_lookups()
        self.addCleanup(dcc.forget_library_lookups)
        key = ("main", "gone.flac")
        dcc._note_lookup_hit(key, os.path.join(os.path.dirname(__file__), "no-such-file.flac"))

        self.assertIsNone(dcc._remembered_path(key))
        self.assertNotIn(key, dcc._lookup_hits, "the stale entry is forgotten, not kept")


if __name__ == "__main__":
    unittest.main()
