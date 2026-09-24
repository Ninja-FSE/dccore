"""#923: searches and file requests keep working while the list rebuilds.

The new list is built under temporary names and swapped in at the end, so
through the scan, the audio-info reading and the writing the published list is
complete and exactly what users have. PAUSE_ON_UPDATE now pauses only the swap
- the rebuild says "publishing" first - and PAUSE_FOR_WHOLE_UPDATE brings the
old whole-rebuild pause back.
"""

import os
import sys
import threading
import time
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import platform_compat  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket, silence_debug  # noqa: E402
from tests.test_webserver import write_master_list  # noqa: E402


class RebuildCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(PAUSE_ON_UPDATE=True, PAUSE_FOR_WHOLE_UPDATE=False,
                        update_inprogress=True, search_inprogress=False)

    def phase(self, name):
        update_list.write_progress(name, force=True)


class WhenTheyWait(RebuildCase):
    def test_not_while_the_published_list_is_untouched(self):
        for name in update_list.PHASES_BEFORE_THE_SWAP:
            self.phase(name)
            self.assertFalse(list_mod.rebuild_pauses_requests(), name)

    def test_while_the_new_list_is_swapped_in(self):
        self.phase("publishing")
        self.assertTrue(list_mod.rebuild_pauses_requests())

    def test_when_the_rebuild_says_nothing_readable_it_pauses_as_it_always_did(self):
        update_list.clear_progress()
        self.assertTrue(list_mod.rebuild_pauses_requests())
        with open(update_list.progress_path(), "w", encoding="utf-8") as handle:
            handle.write("{half a json")
        self.assertTrue(list_mod.rebuild_pauses_requests())

    def test_the_old_whole_rebuild_pause_is_one_setting_away(self):
        self.set_config(PAUSE_FOR_WHOLE_UPDATE=True)
        self.phase("scanning")
        self.assertTrue(list_mod.rebuild_pauses_requests())

    def test_never_when_nothing_is_rebuilding_or_the_pause_is_off(self):
        self.phase("publishing")
        self.set_config(update_inprogress=False)
        self.assertFalse(list_mod.rebuild_pauses_requests())
        self.set_config(update_inprogress=True, PAUSE_ON_UPDATE=False, PAUSE_FOR_WHOLE_UPDATE=True)
        self.assertFalse(list_mod.rebuild_pauses_requests())


class AtSearchAndRequest(RebuildCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        write_master_list(self.tree.lists, "DCCoreTest", [(None, [("Example Song.flac", "4.1MB")])])
        self.set_config(FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest", CHANNEL="#chan")

    def find(self):
        self.oserve.queued.clear()
        list_mod.execute_search(RecordingSocket(), "dave", "example song", "#chan")
        return "".join(m for _u, m, *_ in self.oserve.queued)

    def request(self):
        self.oserve.queued.clear()
        dcc.handle_download_request(RecordingSocket(), "dave", "Example Song.flac", "#chan")
        return "".join(m for _u, m, *_ in self.oserve.queued)

    def test_a_search_during_the_scan_is_answered_from_the_current_list(self):
        self.phase("scanning")
        reply = self.find()
        self.assertIn("Example Song.flac", reply)
        self.assertNotIn("temporarily paused", reply)

    def test_a_search_during_the_swap_waits(self):
        self.phase("publishing")
        self.assertIn("Search engine is temporarily paused", self.find())

    def test_a_request_during_the_scan_is_not_refused_for_the_rebuild(self):
        self.phase("audio")
        self.assertNotIn("MasterList is currently rebuilding", self.request())

    def test_a_request_during_the_swap_is_told_seconds(self):
        self.phase("publishing")
        self.assertIn("File requests temporarily paused. Please try again in a few seconds.", self.request())

    def test_the_whole_rebuild_pause_still_says_minutes(self):
        self.set_config(PAUSE_FOR_WHOLE_UPDATE=True)
        self.phase("scanning")
        self.assertIn("Please wait 1-2 minutes.", self.request())


class TheRebuildSaysWhenItSwaps(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, LIST_BASE_NAME="DCCoreTest",
                        NICKNAME="DCCoreTest", ORIGINAL_NICK="DCCoreTest",
                        RAR_ENABLED=False, LIST_FORMAT="txt")

    def test_publishing_is_said_before_the_swap(self):
        seen = []
        real = update_list._publish_artifacts

        def publish(swaps):
            seen.append(update_list.read_phase())
            return real(swaps)

        update_list._publish_artifacts = publish
        self.addCleanup(setattr, update_list, "_publish_artifacts", real)
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            self.assertTrue(update_list.generate_master_list())
        self.assertEqual(seen, ["publishing"])

    def test_the_swap_waits_for_a_reader_longer_than_it_used_to(self):
        """A search that started just before the swap may still hold the list
        open; on Windows the rename waits for it."""
        attempts = []
        real = platform_compat.replace_with_retry

        def replace(src, dst, **kwargs):
            attempts.append(kwargs.get("attempts"))
            return real(src, dst, **kwargs)

        platform_compat.replace_with_retry = replace
        self.addCleanup(setattr, platform_compat, "replace_with_retry", real)
        os.makedirs(self.tree.lists, exist_ok=True)
        swaps = []
        for name in ("DCCoreTest-list.txt", "DCCoreTest-list.zip"):
            live = os.path.join(self.tree.lists, name)
            for path, text in ((live, "old"), (live + ".new", "new")):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
            swaps.append((live + ".new", live))
        self.assertTrue(update_list._publish_artifacts(swaps))
        self.assertEqual(len(attempts), 4, "moved aside and replaced, for each of two")
        self.assertTrue(all(a == update_list.PUBLISH_REPLACE_ATTEMPTS for a in attempts), attempts)
        self.assertGreaterEqual(update_list.PUBLISH_REPLACE_ATTEMPTS, 10)


class _InlineThread:
    """threading.Thread that runs its target inline."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


class StartingARebuild(DCCoreTestCase):
    """What handle_list_update_request() holds while the rebuild runs."""

    def setUp(self):
        super().setUp()
        silence_debug(announce)
        real_thread = threading.Thread
        threading.Thread = _InlineThread
        self.addCleanup(setattr, threading, "Thread", real_thread)
        real_sleep = time.sleep
        time.sleep = lambda *_a, **_k: None
        self.addCleanup(setattr, time, "sleep", real_sleep)
        self.during = []

        def runner(argv, **kwargs):
            self.during.append(config.search_inprogress)
            return types.SimpleNamespace(returncode=0, stdout="List of 1 Files\n", stderr="")

        real = commands.run_watching_for_a_stall
        commands.run_watching_for_a_stall = runner
        self.addCleanup(setattr, commands, "run_watching_for_a_stall", real)
        self.set_config(PAUSE_ON_UPDATE=True, update_inprogress=False, search_inprogress=False)

    def test_by_default_it_holds_no_search_lock(self):
        self.set_config(PAUSE_FOR_WHOLE_UPDATE=False)
        commands.handle_list_update_request("admin", "#chan", authorised=True)
        self.assertEqual(self.during, [False])

    def test_a_running_search_does_not_refuse_it(self):
        self.set_config(PAUSE_FOR_WHOLE_UPDATE=False, search_inprogress=True)
        commands.handle_list_update_request("admin", "#chan", authorised=True)
        self.assertEqual(len(self.during), 1, "the rebuild ran")
        self.assertTrue(config.search_inprogress, "and left the search's own flag alone")

    def test_the_whole_rebuild_pause_still_takes_it(self):
        self.set_config(PAUSE_FOR_WHOLE_UPDATE=True)
        commands.handle_list_update_request("admin", "#chan", authorised=True)
        self.assertEqual(self.during, [True])
        self.assertFalse(config.search_inprogress, "and gives it back")


if __name__ == "__main__":
    unittest.main()
