"""One nick cannot keep the library scans to itself (#969).

A file request for a name that is not in a folder's root costs a scan of the
whole library, and only MAX_CONCURRENT_LIBRARY_SCANS run at once. The bound on
how often one nick could start one was the flood gate - until #888 took file
requests out of it, so a pasted batch would not mute the user it was for. One
nick sending made-up names as fast as the server allowed then kept both slots
busy, and everybody else was told "busy".

Now a nick's lookups take turns, the next one first looking in what its last
one just learned, and a nick whose last LOOKUP_NICK_MISSES scans found nothing
within a minute is told "busy" without another. Scans that found the file do
not count.
"""

import os
import unittest
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import dcc  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_an_unknown_filename_does_not_scan_the_library_unbounded as unbounded  # noqa: E402


class NickCase(unbounded.LookupBase):
    def setUp(self):
        super().setUp()
        # A turn left behind would make every later request of that nick
        # wait: fail fast, and start each test with none.
        patch = mock.patch.object(dcc, "LOOKUP_SCAN_WAIT_SECONDS", 0.5)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(dcc._scanning_nicks.clear)

    def made_up(self, count, prefix="Nothing Here"):
        return [f"{prefix} {n:02d}.flac" for n in range(count)]

    def a_track(self, index=0):
        return os.path.basename(self.tree.tracks[index])


class AFewMissesAMinute(NickCase):
    def test_a_nick_inventing_names_runs_out_of_scans(self):
        """The audit's case: distinct made-up names, one after another."""
        allowed = dcc.LOOKUP_NICK_MISSES
        for name in self.made_up(allowed + 3):
            self.ask(name, user="dave")
        self.assertEqual(len(self.walks), allowed)
        self.assertEqual(self.errors(), ["file_not_found"] * allowed + ["busy"] * 3)

    def test_everyone_else_is_still_served(self):
        for name in self.made_up(dcc.LOOKUP_NICK_MISSES + 1):
            self.ask(name, user="dave")
        walks = len(self.walks)
        self.notices.clear()
        self.ask(self.a_track(), user="erin")
        self.assertEqual(len(self.walks), walks + 1, "erin's lookup ran")
        self.assertEqual(self.errors(), [])

    def test_the_window_moves_on(self):
        for name in self.made_up(dcc.LOOKUP_NICK_MISSES):
            self.ask(name, user="dave")
        for nick in list(dcc._nick_misses):
            dcc._nick_misses[nick] = [when - dcc.LOOKUP_NICK_MISS_WINDOW_SECONDS - 1
                                      for when in dcc._nick_misses[nick]]
        walks = len(self.walks)
        self.ask("Something New.flac", user="dave")
        self.assertEqual(len(self.walks), walks + 1)

    def test_finding_files_is_never_held_back(self):
        with mock.patch.object(dcc, "LOOKUP_NICK_MISSES", 1):
            for _ in range(3):
                # Forget where it was, not what dave's lookups came to.
                dcc._lookup_hits.clear()
                dcc._lookup_folders.clear()
                self.ask(self.a_track(), user="dave")
        self.assertEqual(self.errors(), [])
        self.assertEqual(len(self.walks), 3, "each one scanned, and each one found it")


class OneScanAtATime(NickCase):
    def test_a_nick_with_a_scan_running_waits_for_it(self):
        self.assertTrue(dcc._take_a_scan_turn("dave", 0))
        self.addCleanup(dcc._end_scan_turn, "dave")
        with mock.patch.object(dcc, "LOOKUP_SCAN_WAIT_SECONDS", 0.05):
            self.ask("Something Else.flac", user="dave")
            self.assertEqual((self.walks, self.errors()), ([], ["busy"]))
            self.ask("Something Else.flac", user="erin")
        self.assertEqual(len(self.walks), 1, "another nick is not in dave's queue")

    def test_the_turn_is_given_back(self):
        self.ask("Nothing Here.flac", user="dave")
        self.ask(self.a_track(), user="dave")
        self.assertEqual(dcc._scanning_nicks, set())
        self.assertEqual(self.errors(), ["file_not_found"])

    def test_what_the_last_lookup_learned_answers_the_next(self):
        """A pasted batch: while dave waited, his previous lookup found the
        first track - the second, its sibling, is found without a scan."""
        real_turn = dcc._take_a_scan_turn

        def turn(nick, timeout):
            taken = real_turn(nick, timeout)
            if nick == "dave":
                # The lookup that was running when this one began, finishing.
                self.ask(self.a_track(0), user="erin")
            return taken

        with mock.patch.object(dcc, "_take_a_scan_turn", turn):
            self.ask(self.a_track(1), user="dave")
        self.assertEqual(len(self.walks), 1, "only the first lookup scanned")
        self.assertEqual(self.errors(), [])

    def test_a_miss_the_last_lookup_found_is_not_scanned_again(self):
        real_turn = dcc._take_a_scan_turn

        def turn(nick, timeout):
            taken = real_turn(nick, timeout)
            if nick == "dave":
                self.ask("Same Stale Row.flac", user="erin")
            return taken

        with mock.patch.object(dcc, "_take_a_scan_turn", turn):
            self.ask("Same Stale Row.flac", user="dave")
        self.assertEqual(len(self.walks), 1)
        self.assertEqual(self.errors(), ["file_not_found"] * 2)


if __name__ == "__main__":
    unittest.main()
