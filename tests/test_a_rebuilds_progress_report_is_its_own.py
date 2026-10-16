"""A rebuild's progress report is its own, and there from its first step (#1361).

Three ways the "rebuilding" state could go wrong, found by checking every way
a rebuild can end:

1. A run killed for stalling, or ended by a crash or a reboot, never cleared
   its progress file. The next rebuild's watch read that old report as its
   own child's, "silent for hours", and killed it at the first tick - before
   it had written anything - and the one after, every time.
2. A rebuild that blocked before its first report (statting its folders on a
   mount that stopped answering) was never ended, since with no report the
   watch cannot tell a stall from a slow start; and with no phase to read,
   every search and file request was refused meanwhile, until a restart.
3. Between raising the flags and starting the rebuild's thread nothing took
   them down again on an exception.

And the follow-ups from the review (#1366): a report with no token counted as
the child's, a reading nobody watches ran on, the announcements before the
guard could still leave the flags up, the reading's own stall watch read any
run's report, and a rebuild that never started recorded nothing.

Every nick, path and token here is invented.
"""

import contextlib
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import runtime  # noqa: E402
import subprocess  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_a_working_rebuild_is_not_hung import FakeChild  # noqa: E402

TOKEN = "a1b2c3d4e5f6a1b2c3d4e5f6"
OTHER = "ffeeddccbbaaffeeddccbbaa"


class ProgressCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.progress = os.path.join(self.tree.root, "progress.json")
        self.set_config(LIST_PROGRESS_FILE=self.progress)

    def reported(self, seconds_ago, token, phase="scanning"):
        with io.open(self.progress, "w", encoding="utf-8") as handle:
            json.dump({"phase": phase, "at": time.time() - seconds_ago, "token": token}, handle)


class WhoseReportItIs(ProgressCase):

    def test_another_runs_report_is_not_this_childs(self):
        self.reported(1200, OTHER)
        self.assertIsNone(commands.last_progress_at(token=TOKEN))

    def test_this_childs_report_is_read(self):
        self.reported(5, TOKEN)
        self.assertAlmostEqual(commands.last_progress_at(token=TOKEN), time.time() - 5, delta=2)

    def test_a_report_with_no_token_is_not_this_childs_either(self):
        """A terminal's run that crashed (#1366): the bot's child always
        stamps the token it was given."""
        self.reported(5, "")
        self.assertIsNone(commands.last_progress_at(token=TOKEN))

    def test_without_a_token_to_compare_every_report_is_read(self):
        self.reported(5, OTHER)
        self.assertIsNotNone(commands.last_progress_at())
        self.reported(5, "")
        self.assertIsNotNone(commands.last_progress_at())


class TheWatchAndALeftoverReport(ProgressCase):
    """run_watching_for_a_stall() with the token the bot gives the child."""

    def setUp(self):
        super().setUp()
        real_popen = subprocess.Popen
        self.addCleanup(setattr, subprocess, "Popen", real_popen)

    def install(self, child):
        subprocess.Popen = lambda *a, **k: child
        return child

    def test_a_leftover_report_does_not_kill_the_next_rebuild(self):
        """The old run's report is twenty minutes old; this child has not
        written yet. It is left alone and finishes."""
        child = self.install(FakeChild(ticks=3, returncode=0))
        self.reported(1200, OTHER)
        result = commands.run_watching_for_a_stall(
            ["x"], stall=900, tick=0.01, env={update_list.RUN_TOKEN_ENV: TOKEN})
        self.assertFalse(child.killed)
        self.assertEqual(result.returncode, 0)

    def test_its_own_silence_is_still_a_stall(self):
        child = self.install(FakeChild(ticks=None))
        self.reported(1200, TOKEN)
        with self.assertRaises(commands.ListUpdateStalled):
            commands.run_watching_for_a_stall(
                ["x"], stall=900, tick=0.01, env={update_list.RUN_TOKEN_ENV: TOKEN})
        self.assertTrue(child.killed)

    def test_a_stalled_child_leaves_no_report_behind(self):
        self.install(FakeChild(ticks=None))
        self.reported(1200, TOKEN)
        with self.assertRaises(commands.ListUpdateStalled):
            commands.run_watching_for_a_stall(
                ["x"], stall=900, tick=0.01, env={update_list.RUN_TOKEN_ENV: TOKEN})
        self.assertFalse(os.path.exists(self.progress))

    def test_a_child_past_the_ceiling_leaves_no_report_behind(self):
        self.install(FakeChild(ticks=None))
        self.reported(0, TOKEN)
        with self.assertRaises(subprocess.TimeoutExpired):
            commands.run_watching_for_a_stall(["x"], ceiling=0.001, stall=900, tick=0.01)
        self.assertFalse(os.path.exists(self.progress))


class TheFirstReport(ProgressCase):
    """rebuild_main() reports "starting" before it looks at any folder."""

    def setUp(self):
        super().setUp()
        self.missing = os.path.join(self.tree.root, "unplugged")
        self.set_config(FILE_DIRECTORY=self.missing)

    def run_main(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return update_list.rebuild_main(log=print)

    def test_it_is_written_before_the_folders_are_looked_at(self):
        seen = []
        real = update_list.platform_compat.long_path

        def long_path(path):
            if path == self.missing:
                seen.append(update_list.read_phase())
            return real(path)

        with mock.patch.object(update_list.platform_compat, "long_path", long_path):
            self.assertEqual(self.run_main(), 1)
        self.assertEqual(seen, [update_list.STARTING_PHASE])

    def test_a_run_that_stops_at_the_checks_leaves_no_report(self):
        self.assertEqual(self.run_main(), 1)
        self.assertFalse(os.path.exists(self.progress))

    def test_no_folder_configured_leaves_no_report_either(self):
        self.set_config(FILE_DIRECTORY="")
        with mock.patch("library.lists", return_value=[]):
            self.assertEqual(self.run_main(), 1)
        self.assertFalse(os.path.exists(self.progress))

    def test_it_never_writes_over_a_running_rebuilds_report(self):
        """Refused for the lock first: the report there is the other run's."""
        self.reported(3, OTHER, phase="publishing")
        with open(self.progress, encoding="utf-8") as handle:
            before = handle.read()
        with mock.patch.object(update_list, "take_list_lock", return_value=(4321, "rebuild")):
            self.assertEqual(self.run_main(), 1)
        with open(self.progress, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_starting_pauses_nothing(self):
        """Searches and file requests are answered from the current list
        while a rebuild starts, as they are while it scans."""
        self.set_config(update_inprogress=True, PAUSE_ON_UPDATE=True, PAUSE_FOR_WHOLE_UPDATE=False)
        self.reported(0, TOKEN, phase=update_list.STARTING_PHASE)
        self.assertFalse(list_mod.rebuild_pauses_requests())
        self.reported(0, TOKEN, phase="publishing")
        self.assertTrue(list_mod.rebuild_pauses_requests())


class NothingLeavesTheFlagsUp(DCCoreTestCase):
    """An exception between raising the flags and starting the thread."""

    def setUp(self):
        super().setUp()
        self.set_config(update_inprogress=False, search_inprogress=False,
                        PAUSE_ON_UPDATE=True, PAUSE_FOR_WHOLE_UPDATE=True)

    def request(self):
        with contextlib.redirect_stdout(io.StringIO()):
            commands.handle_list_update_request("someadmin", "#somechannel", authorised=True)

    def test_a_thread_that_cannot_start(self):
        with mock.patch("threading.Thread.start", side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                self.request()
        self.assertFalse(config.update_inprogress)
        self.assertFalse(config.search_inprogress)

    def test_the_count_before_it_failing(self):
        with mock.patch.object(commands, "count_by_list", side_effect=OSError("mount gone")):
            with self.assertRaises(OSError):
                self.request()
        self.assertFalse(config.update_inprogress)
        self.assertFalse(config.search_inprogress)

    def test_a_reading_whose_watch_cannot_start_is_not_left_running(self):
        def cannot(*_a, **_k):
            raise RuntimeError("can't start new thread")
        with self.assertRaises(RuntimeError):
            commands.start_audio_watch(FakeChild(ticks=None), "rebuild", start=cannot)
        self.assertIsNone(runtime.audio_reading)


class InlineThread:
    """threading.Thread whose start() runs the target here, to the end."""

    def __init__(self, target=None, daemon=None, **_kwargs):
        self._target = target

    def start(self):
        self._target()


class ACrashedHandRunDoesNotKillTheNextUpdate(ProgressCase):
    """#1366 item 1: a hand-run update_list.py that crashed mid-scan leaves a
    report with no token. The bot's next child had not written yet, and was
    killed at the watch's first ticks as silent for twenty minutes."""

    def setUp(self):
        super().setUp()
        real_popen = subprocess.Popen
        self.addCleanup(setattr, subprocess, "Popen", real_popen)
        self.set_config(update_inprogress=False, search_inprogress=False,
                        PAUSE_ON_UPDATE=True, PAUSE_FOR_WHOLE_UPDATE=False,
                        last_list_update_ok=None, last_list_update_error=None)

    def test_the_watch_leaves_the_new_child_alone(self):
        child = FakeChild(ticks=3, returncode=0)
        subprocess.Popen = lambda *a, **k: child
        self.reported(1200, "")
        result = commands.run_watching_for_a_stall(
            ["x"], stall=900, tick=0.01, env={update_list.RUN_TOKEN_ENV: TOKEN})
        self.assertFalse(child.killed)
        self.assertEqual(result.returncode, 0)

    def start_rebuild(self, holder):
        """!update with the thread run here; what the progress file held at
        the moment the child would have started is the answer."""
        seen = []

        def watch(*_a, **_k):
            seen.append(os.path.exists(self.progress))
            raise commands.ListUpdateStalled(1)

        with mock.patch("threading.Thread", InlineThread), \
                mock.patch.object(commands, "run_watching_for_a_stall", watch), \
                mock.patch.object(commands, "count_by_list", return_value={}), \
                mock.patch.object(update_list, "list_lock_holder", return_value=holder), \
                contextlib.redirect_stdout(io.StringIO()):
            commands.handle_list_update_request("someadmin", "#somechannel", authorised=True)
        return seen

    def test_the_leftover_report_is_gone_before_the_child_starts(self):
        self.reported(1200, "")
        self.assertEqual(self.start_rebuild(holder=None), [False])

    def test_a_report_of_a_run_that_holds_the_list_is_left(self):
        self.reported(1, "", phase="scanning")
        self.assertEqual(self.start_rebuild(holder=(4321, "rebuild")), [True])


class AReadingNobodyWatchesIsEnded(ProgressCase):
    """#1366 item 2: the watch's thread could not start. The child ran on with
    nobody draining its pipes, holding the list lock."""

    def test_the_child_is_killed_and_its_report_dropped(self):
        child = FakeChild(ticks=None)
        self.reported(0, TOKEN, phase=update_list.READING_PHASE)

        def cannot(*_a, **_k):
            raise RuntimeError("can't start new thread")

        with self.assertRaises(RuntimeError):
            commands.start_audio_watch(child, "rebuild", start=cannot, token=TOKEN)
        self.assertTrue(child.killed)
        self.assertFalse(os.path.exists(self.progress))
        self.assertIsNone(runtime.audio_reading)

    def test_a_child_that_cannot_be_killed_does_not_hide_the_first_error(self):
        child = FakeChild(ticks=None)
        child.kill = mock.Mock(side_effect=OSError("access denied"))

        def cannot(*_a, **_k):
            raise RuntimeError("can't start new thread")

        with self.assertRaisesRegex(RuntimeError, "new thread"):
            commands.start_audio_watch(child, "rebuild", start=cannot, token=TOKEN)


class TheReadingsStallWatchReadsItsOwnReport(ProgressCase):
    """#1366 item 4: watch_audio_reading() read any run's report, so another
    run's twenty-minute-old one killed a reading that had just begun."""

    def watch(self, child, token):
        import threading
        import types
        job = types.SimpleNamespace(process=child, kind="rebuild", started=time.time(),
                                    done=threading.Event(), stopping=False, token=token)
        with contextlib.redirect_stdout(io.StringIO()):
            commands.watch_audio_reading(job, tick=0.01)

    def test_another_runs_old_report_does_not_stop_it(self):
        self.set_config(LIST_UPDATE_STALL_SECONDS=900)
        child = FakeChild(ticks=3, returncode=0)
        self.reported(1200, OTHER)
        self.watch(child, TOKEN)
        self.assertFalse(child.killed)

    def test_its_own_silence_still_does(self):
        self.set_config(LIST_UPDATE_STALL_SECONDS=900)
        child = FakeChild(ticks=None)
        self.reported(1200, TOKEN)
        self.watch(child, TOKEN)
        self.assertTrue(child.killed)


class ARebuildThatNeverStarted(DCCoreTestCase):
    """#1366 items 3 and 5: the announcements between the gate and the guard
    were unguarded, and a rebuild that failed there recorded nothing."""

    def setUp(self):
        super().setUp()
        self.set_config(update_inprogress=False, search_inprogress=False,
                        PAUSE_ON_UPDATE=True, PAUSE_FOR_WHOLE_UPDATE=True,
                        last_list_update_ok=True, last_list_update_error=None)

    def request(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                commands.handle_list_update_request("someadmin", "#somechannel", authorised=True)
            finally:
                self.printed = out.getvalue()

    def test_an_announcement_that_fails_leaves_no_flag_up(self):
        import announce
        with mock.patch.object(announce, "send_debug", side_effect=RuntimeError("socket gone")):
            with self.assertRaises(RuntimeError):
                self.request()
        self.assertFalse(config.update_inprogress)
        self.assertFalse(config.search_inprogress)

    def test_a_failed_count_is_recorded_as_a_failed_rebuild(self):
        with mock.patch.object(commands, "count_by_list", side_effect=OSError("mount gone")):
            with self.assertRaises(OSError):
                self.request()
        self.assertIs(config.last_list_update_ok, False)
        self.assertIn("could not start", config.last_list_update_error)
        self.assertIn("mount gone", config.last_list_update_error)
        self.assertIn("[MAINTENANCE END]", self.printed)

    def test_a_thread_that_cannot_start_is_recorded_too(self):
        with mock.patch.object(commands, "count_by_list", return_value={}), \
                mock.patch("threading.Thread.start", side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                self.request()
        self.assertIs(config.last_list_update_ok, False)
        self.assertIn("can't start new thread", config.last_list_update_error)


class NothingConfiguredIsRefusedBeforeTheLock(ProgressCase):
    """#1366 item 6: a run with no folder at all took the lock first, and so
    stopped a running audio reading and waited for it, only to refuse."""

    def test_the_lock_is_not_asked_for(self):
        self.set_config(FILE_DIRECTORY="")
        with mock.patch("library.lists", return_value=[]), \
                mock.patch.object(update_list, "take_list_lock") as take, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(update_list.rebuild_main(log=print), 1)
        take.assert_not_called()

    def test_a_reading_s_report_is_left_alone(self):
        self.set_config(FILE_DIRECTORY="")
        self.reported(1, OTHER, phase=update_list.READING_PHASE)
        with mock.patch("library.lists", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(update_list.rebuild_main(log=print), 1)
        self.assertTrue(os.path.exists(self.progress))


if __name__ == "__main__":
    unittest.main()
