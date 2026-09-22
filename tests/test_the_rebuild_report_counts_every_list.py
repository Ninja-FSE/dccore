"""#873: the rebuild report counted only the primary list.

`!update` (and the dashboard's Rebuild) ends with a line saying how many files
the library has and how many are new. It read the count without a list name, which
means the primary list, so a second list - films and series, or whatever the
operator called it - was rebuilt but never reported, and the "file count DROPPED"
guard could not see a folder that lost its mount. It now counts every configured
list and names each one by the name the operator gave it.
"""

import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import library  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402
from tests.test_commands import _SyncThread  # noqa: E402


def served(name, primary=False):
    return library.ServedList(name, primary, [], [])


class TheCounts(DCCoreTestCase):

    def with_lists(self, names_and_counts):
        entries = [served(name, primary=(index == 0)) for index, (name, _c) in enumerate(names_and_counts)]
        counts = dict(names_and_counts)
        patches = [
            mock.patch.object(library, "lists", lambda: entries),
            mock.patch("list.get_file_count_date_size_and_raw_bytes",
                       lambda name=None: (counts[name], "Sep 21st", "1B", 1)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_every_configured_list_is_counted_by_the_name_it_was_given(self):
        self.with_lists([("Main", 64136), ("video", 18204)])
        self.assertEqual(commands.count_by_list(), [("Main", 64136), ("video", 18204)])

    def test_other_operators_names_are_used_as_they_are(self):
        self.with_lists([("music", 10), ("tv", 20), ("movies", 30)])
        self.assertEqual([name for name, _c in commands.count_by_list()], ["music", "tv", "movies"])

    def test_one_list_that_cannot_be_read_counts_zero_and_the_rest_are_still_counted(self):
        entries = [served("Main", True), served("video")]

        def counts(name=None):
            if name == "video":
                raise OSError("share gone")
            return (5, "d", "1B", 1)

        with mock.patch.object(library, "lists", lambda: entries), \
                mock.patch("list.get_file_count_date_size_and_raw_bytes", counts), \
                mock.patch("builtins.print"):
            self.assertEqual(commands.count_by_list(), [("Main", 5), ("video", 0)])

    def test_no_lists_file_is_one_list_called_main(self):
        with mock.patch("list.get_file_count_date_size_and_raw_bytes", lambda name=None: (7, "d", "1B", 1)):
            self.assertEqual(commands.count_by_list(), [("Main", 7)])


class TheSentence(unittest.TestCase):

    def test_one_list_keeps_the_wording_it_always_had(self):
        text, shrunk = commands.describe_list_counts([("Main", 100)], [("Main", 112)])
        self.assertEqual(text, "MasterList now contains 112 files. Added 12 new file(s) since last index.")
        self.assertEqual(shrunk, [])

    def test_several_lists_are_named_one_by_one_with_what_each_gained(self):
        text, shrunk = commands.describe_list_counts([("Main", 64136), ("video", 18192)],
                                                     [("Main", 64136), ("video", 18204)])
        self.assertEqual(text, "Main: 64,136 files (+0 new); video: 18,204 files (+12 new).")
        self.assertEqual(shrunk, [])

    def test_a_list_added_since_the_last_time_counts_from_zero(self):
        text, _ = commands.describe_list_counts([("Main", 10)], [("Main", 10), ("tv", 300)])
        self.assertIn("tv: 300 files (+300 new)", text)

    def test_a_list_that_shrank_is_reported_by_name(self):
        _, shrunk = commands.describe_list_counts([("Main", 100), ("video", 50)], [("Main", 100), ("video", 20)])
        self.assertEqual(shrunk, [("video", 50, 20)])

    def test_a_shrunk_single_list_is_reported_too(self):
        _, shrunk = commands.describe_list_counts([("Main", 100)], [("Main", 40)])
        self.assertEqual(shrunk, [("Main", 100, 40)])

    def test_a_negative_change_reads_with_its_sign(self):
        text, _ = commands.describe_list_counts([("a", 5), ("b", 9)], [("a", 5), ("b", 3)])
        self.assertIn("b: 3 files (-6 new)", text)


class TheReport(DCCoreTestCase):
    """Through the real `!update` handler with the rebuild itself stubbed."""

    def setUp(self):
        super().setUp()
        self.debug = silence_debug(announce)
        real_thread = threading.Thread
        threading.Thread = _SyncThread
        self.addCleanup(setattr, threading, "Thread", real_thread)
        real_sleep = time.sleep
        time.sleep = lambda *_a, **_k: None
        self.addCleanup(setattr, time, "sleep", real_sleep)
        self.before = [("Main", 100), ("video", 50)]
        self.after = [("Main", 100), ("video", 62)]
        self.calls = 0

        def counter():
            self.calls += 1
            return list(self.before) if self.calls == 1 else list(self.after)

        for target, value in (
                ("count_by_list", counter),
                ("run_watching_for_a_stall",
                 lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))):
            patch = mock.patch.object(commands, target, value)
            patch.start()
            self.addCleanup(patch.stop)

    def messages(self):
        return [msg for _cat, msg in self.debug]

    def test_the_second_lists_gain_is_in_the_report(self):
        commands.handle_list_update_request("admin", "#chan", authorised=True)
        done = [m for m in self.messages() if "successfully completed" in m]
        self.assertEqual(len(done), 1, self.messages())
        self.assertIn("Main: 100 files (+0 new); video: 62 files (+12 new).", done[0])

    def test_a_shrunk_list_warns_by_name_and_the_summary_still_follows(self):
        self.after = [("Main", 130), ("video", 20)]
        commands.handle_list_update_request("admin", "#chan", authorised=True)
        messages = self.messages()
        drop = [m for m in messages if "DROPPED" in m]
        self.assertEqual(len(drop), 1, messages)
        self.assertIn("'video'", drop[0])
        self.assertIn("50", drop[0])
        self.assertIn("20", drop[0])
        self.assertIn("Check that list's folder/mount", drop[0],
                      "the folder to check is the shrunk list's own, not 'the music directory'")
        self.assertNotIn("music directory", drop[0])
        self.assertTrue(any("Main: 130 files (+30 new)" in m for m in messages),
                        "what the other list gained is not lost behind the warning")
        self.assertFalse(any("successfully completed" in m for m in messages))

    def test_one_list_that_shrank_gets_the_warning_alone_as_before(self):
        self.before, self.after = [("Main", 100)], [("Main", 40)]
        commands.handle_list_update_request("admin", "#chan", authorised=True)
        messages = self.messages()
        drop = [m for m in messages if "DROPPED" in m]
        self.assertEqual(len(drop), 1)
        self.assertNotIn("of the list", drop[0])
        self.assertIn("Check the music directory/mount", drop[0])
        self.assertFalse(any("List update completed in" in m for m in messages))

    def test_one_list_reads_exactly_as_before(self):
        self.before, self.after = [("Main", 100)], [("Main", 112)]
        commands.handle_list_update_request("admin", "#chan", authorised=True)
        self.assertTrue(any("MasterList now contains 112 files. Added 12 new file(s) since last index." in m
                            for m in self.messages()), self.messages())


if __name__ == "__main__":
    unittest.main()
