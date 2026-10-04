"""The background audio reading runs once at a time, and its lengths always
get into the list (#1182, the audit of its first version).

What the audit found, and what is pinned here:
- A list's swap left "publishing" in the progress file into the next list's
  reading, pausing searches and the list archive until a read completed.
- Lengths read but not swapped in - a stop, a failed swap - were never
  written in by Read audio info, which said "nothing new to read".
- On Windows a list download already running made the reading's swap fail,
  and anybody could arrange that. Now new downloads wait from the reading's
  start (not during the reading itself) through its swap, the swap waits for
  a running one, and the bot writes the lengths in once none is running.
- A reading the bot did not start could not be stopped by its rebuild, and
  both staged the same temporary files. Now every run holds a lock beside
  the progress file, a rebuild stops the reading that holds it, a second
  rebuild or reading is refused, and the rewrite stages under its own names.
- A rebuild run by hand blocked for the whole reading with no progress. Now
  it publishes and says how to read; `--read-audio-info` by hand prints
  progress lines.
- A reading outlived a bot that ended without its shutdown. Now it stops by
  itself when that bot is gone, and the next start stops one left running.
"""

import io
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import audio_info  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import platform_compat  # noqa: E402
import runtime  # noqa: E402
import update_list  # noqa: E402

# Modules, not classes: a TestCase imported by name runs again here.
import tests.test_the_list_publishes_first_and_reads_audio_afterwards as first  # noqa: E402

BOT_RUN = {update_list.RUN_TOKEN_ENV: "a-run-of-the-bot"}


class Case(first.Library):
    """first.Library's small library, its env patch included."""

    def quietly(self, call, *args, **kwargs):
        with redirect_stdout(io.StringIO()) as out:
            value = call(*args, **kwargs)
        return value, out.getvalue()

    def hold(self, kind):
        """The list lock held by "another process": another handle."""
        handle = platform_compat.take_file_lock(
            platform_compat.long_path(update_list.list_lock_path()), kind)
        self.addCleanup(platform_compat.release_file_lock, handle)
        return handle


class TheSwapsPhaseDoesNotOutliveIt(Case):

    def test_no_read_of_the_next_list_sees_publishing(self):
        jobs, _result, _said = self.rebuild(read=False)
        (job,) = jobs
        half = len(job.pending) // 2
        # Two lists' worth of work: the first one is written in before the
        # second one reads anything.
        jobs = [job._replace(pending=job.pending[:half]), job._replace(pending=job.pending[half:])]
        seen = []
        real_read = audio_info.read

        def read(path, size=None):
            seen.append(update_list.read_phase())
            return real_read(path, size)

        self.set_config(LIST_AUDIO_INFO_THREADS=1)
        with mock.patch.object(audio_info, "read", read):
            result, _out = self.quietly(update_list.read_audio_info, jobs)
        self.assertEqual(result["updated"], ["Main", "Main"], "both halves were written in")
        self.assertNotIn("publishing", seen)
        self.assertNotIn("packing", seen)

    def test_a_failed_swap_puts_the_reading_phase_back_too(self):
        jobs, _result, _said = self.rebuild(read=False)
        with mock.patch.object(update_list, "READING_SWAP_WAIT", 0), \
                mock.patch.object(update_list, "_publish_artifacts", side_effect=PermissionError("held")):
            result, _out = self.quietly(update_list.read_audio_info, jobs)
        self.assertEqual(result["unwritten"], ["Main"])
        self.assertEqual(update_list.read_phase(), "reading")


class WhatWasReadIsAlwaysWrittenIn(Case):

    def read_but_not_swapped(self):
        jobs, _result, _said = self.rebuild(read=False)
        with mock.patch.object(update_list, "READING_SWAP_WAIT", 0), \
                mock.patch.object(update_list, "_publish_artifacts", side_effect=PermissionError("held")):
            result, _out = self.quietly(update_list.run_audio_reading, jobs)
        self.assertEqual(result["unwritten"], ["Main"])
        self.assertTrue(all(" " not in tail for tail in self.tails().values()))

    def test_read_audio_info_writes_in_what_the_cache_holds(self):
        self.read_but_not_swapped()
        result, out = self.quietly(update_list.run_audio_reading)
        self.assertEqual((result["outcome"], result["total"], result["updated"]), ("done", 0, ["Main"]))
        self.assertIn("Audio info: nothing new to read; lengths read earlier: list updated.", out)
        self.assertRegex(self.tails()["Example Artist - 01 - Opening.mp3"], r" 0m26s 128/44\.1/JS$")

    def test_and_says_nothing_new_when_there_is_nothing(self):
        self.rebuild()
        result, out = self.quietly(update_list.run_audio_reading)
        self.assertEqual(result["outcome"], "nothing")
        self.assertIn("Audio info: nothing new to read.", out)

    def test_a_rebuilds_own_reading_does_not_rewrite_every_list(self):
        """Only the reading-only run looks at every list: a rebuild has just
        published them all from the cache."""
        jobs, _result, _said = self.rebuild(read=False)
        calls = []
        real = update_list.rewrite_audio_info
        with mock.patch.object(update_list, "rewrite_audio_info",
                               lambda *a, **k: calls.append(a) or real(*a, **k)):
            self.quietly(update_list.run_audio_reading, jobs)
        self.assertEqual(len(calls), 1)


class ARunningDownloadIsWaitedFor(Case):

    def setUp(self):
        """The list as a rebuild published it, and every length read into
        the cache but not written in yet."""
        super().setUp()
        jobs, _result, _said = self.rebuild(read=False)
        with mock.patch.object(update_list, "rewrite_audio_info",
                               lambda *a, **k: update_list.REWRITE_UNCHANGED):
            self.quietly(update_list.read_audio_info, jobs, stop=lambda: False)
        self.assertTrue(all(" " not in tail for tail in self.tails().values()))

    def clock(self):
        now = [0.0]
        return now, (lambda: now[0]), (lambda seconds: now.__setitem__(0, now[0] + seconds))

    def test_the_swap_is_tried_again_while_the_list_is_held(self):
        real = update_list._publish_artifacts
        failures = [PermissionError("held"), PermissionError("held")]

        def publish(swaps):
            if failures:
                raise failures.pop()
            return real(swaps)

        _now, clock, sleep = self.clock()
        with mock.patch.object(update_list, "_publish_artifacts", publish):
            answer, out = self.quietly(update_list.rewrite_audio_info, clock=clock, sleep=sleep)
        self.assertEqual(answer, update_list.REWRITE_UPDATED)
        self.assertIn("The list is open elsewhere", out)
        self.assertRegex(self.tails()["Example Artist - 01 - Opening.mp3"], r" 0m26s")

    def test_but_not_for_ever(self):
        now, clock, sleep = self.clock()
        with mock.patch.object(update_list, "_publish_artifacts", side_effect=PermissionError("held")):
            answer, _out = self.quietly(update_list.rewrite_audio_info, swap_wait=60,
                                        clock=clock, sleep=sleep)
        self.assertEqual(answer, update_list.REWRITE_FAILED)
        self.assertGreaterEqual(now[0], 60)
        self.assertEqual([n for n in os.listdir(self.tree.lists) if n.endswith(".new")], [])

    def test_a_stop_while_waiting_ends_it(self):
        _now, clock, sleep = self.clock()
        asked = iter([False, False, True])
        with mock.patch.object(update_list, "_publish_artifacts", side_effect=PermissionError("held")):
            answer, _out = self.quietly(update_list.rewrite_audio_info, stop=lambda: next(asked),
                                        clock=clock, sleep=sleep)
        self.assertEqual(answer, update_list.REWRITE_STOPPED)


class TheBotWritesItInOnceNoDownloadRuns(first.Library):

    def setUp(self):
        super().setUp()
        self.started = []
        real = commands.handle_audio_info_request
        commands.handle_audio_info_request = lambda *a, **k: self.started.append(a) or ("started", "")
        self.addCleanup(setattr, commands, "handle_audio_info_request", real)
        self.naps = []

    def retry(self):
        thread = []
        self.assertTrue(commands.retry_audio_rewrite_when_free(
            start=lambda target: thread.append(target), sleep=self.naps.append,
            clock=lambda: len(self.naps)))
        return thread[0]

    def test_it_waits_for_the_list_downloads_then_starts_a_reading(self):
        config.active_transfers.append({"user": "dave", "file": "SomeBot-2026-10-04.zip"})
        config.active_transfers.append({"user": "erin", "file": "Example Artist - 01 - Opening.mp3"})

        def nap(seconds):
            self.naps.append(seconds)
            if len(self.naps) == 3:
                del config.active_transfers[0]   # the list download ended

        captured = []
        self.assertTrue(commands.retry_audio_rewrite_when_free(
            start=captured.append, sleep=nap, clock=lambda: len(self.naps)))
        captured[0]()
        self.assertEqual(len(self.started), 1, "once, when the list was free")
        self.assertEqual(len(self.naps), 3, "not while the list download ran")
        self.assertFalse(runtime.audio_retry_waiting)

    def test_it_gives_up_after_a_day(self):
        config.active_transfers.append({"user": "dave", "file": "SomeBot-2026-10-04.zip"})
        captured = []
        with mock.patch.object(commands, "AUDIO_RETRY_GIVE_UP_SECONDS", 5), redirect_stdout(io.StringIO()):
            commands.retry_audio_rewrite_when_free(start=captured.append, sleep=self.naps.append,
                                                   clock=lambda: len(self.naps))
            captured[0]()
        self.assertEqual(self.started, [])
        self.assertFalse(runtime.audio_retry_waiting)

    def test_a_rebuild_meanwhile_makes_it_moot(self):
        target = self.retry()
        self.set_config(update_inprogress=True)
        target()
        self.assertEqual(self.started, [])
        self.assertFalse(runtime.audio_retry_waiting)

    def test_one_wait_at_a_time(self):
        self.retry()
        self.assertFalse(commands.retry_audio_rewrite_when_free(start=lambda target: None))

    def test_the_watcher_starts_it_when_a_list_was_not_written(self):
        import tests.test_a_rebuild_hands_the_audio_reading_to_the_background as daemon_side
        started = []
        with mock.patch.object(commands, "retry_audio_rewrite_when_free", lambda: started.append(True)), \
                mock.patch("announce.send_debug", lambda *a, **k: None):
            job = commands.start_audio_watch(daemon_side.Child(ticks=0, stdout=daemon_side.result_line(
                outcome="done", read=5, unreadable=0, total=5, updated=[], unwritten=["Main"])),
                "rebuild", start=lambda: None)
            commands.watch_audio_reading(job, tick=0.01)
        self.assertEqual(started, [True])
        self.assertIn("written in later", runtime.audio_reading_last["message"])


class OneAtATime(Case):

    def test_a_rebuild_stops_a_reading_it_did_not_start(self):
        holder = self.hold("reading")

        def obey():
            # Lets go only when asked: a rebuild that did not ask would wait
            # in vain.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if update_list.stop_requested():
                    platform_compat.release_file_lock(holder)
                    return
                time.sleep(0.01)

        reader = threading.Thread(target=obey, daemon=True)
        reader.start()
        said = []
        self.assertIsNone(update_list.take_list_lock("rebuild", log=said.append, wait=5))
        self.addCleanup(update_list.release_list_lock)
        reader.join(10)
        self.assertIn("asking it to stop", said[0])
        self.assertFalse(update_list.stop_requested(), "the request is gone with the reading")

    def test_one_that_does_not_stop_keeps_the_rebuild_out(self):
        self.hold("reading")
        holder = update_list.take_list_lock("rebuild", log=lambda line: None, wait=0.6)
        self.assertEqual(holder[1], "reading")
        self.assertFalse(update_list.stop_requested())

    def test_a_second_rebuild_is_refused_at_once(self):
        self.hold("rebuild")
        said = []
        with mock.patch.object(update_list, "request_stop",
                               side_effect=AssertionError("a rebuild is not asked to stop")):
            code = update_list.rebuild_main(log=said.append)
        self.assertEqual(code, 1)
        self.assertTrue(any("Another list rebuild is already running" in line for line in said), said)

    def test_a_reading_only_run_is_refused_while_anything_holds_it(self):
        self.rebuild(read=False)
        for kind in ("rebuild", "reading"):
            handle = self.hold(kind)
            with mock.patch.object(audio_info, "read", side_effect=AssertionError("read")), \
                    mock.patch.object(update_list, "request_stop",
                                      side_effect=AssertionError("a reading-only run stops nobody")):
                result, out = self.quietly(update_list.read_audio_info_main)
            self.assertEqual(result["outcome"], "busy", kind)
            self.assertIn("already running", out)
            platform_compat.release_file_lock(handle)

    def test_the_bot_does_not_start_a_second_reading(self):
        self.hold("reading")
        started = []
        status, message = commands.handle_audio_info_request(
            "admin", "#chan", authorised=True, popen=lambda *a, **k: started.append(a))
        self.assertEqual((status, started), ("running", []))
        self.assertIn("already reading", message)

    def test_the_rewrite_stages_under_its_own_names(self):
        jobs, _result, _said = self.rebuild(read=False)
        rebuilds_own = self.master() + ".new"
        with open(rebuilds_own, "w", encoding="utf-8") as handle:
            handle.write("a rebuild writing its list")
        swapped = []
        real = update_list._publish_artifacts
        with mock.patch.object(update_list, "_publish_artifacts",
                               lambda swaps: swapped.extend(swaps) or real(swaps)):
            self.quietly(update_list.run_audio_reading, jobs)
        self.assertTrue(swapped)
        self.assertTrue(all(source.endswith(update_list.AUDIO_STAGING) for source, _dest in swapped), swapped)
        with open(rebuilds_own, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "a rebuild writing its list", "untouched")

    def test_a_stale_stop_request_is_cleared_when_the_lock_is_taken(self):
        update_list.request_stop()
        self.assertIsNone(update_list.take_list_lock("reading"))
        self.addCleanup(update_list.release_list_lock)
        self.assertFalse(update_list.stop_requested())


class ARebuildRunByHand(Case):

    def test_publishes_and_says_how_to_read(self):
        with mock.patch.dict(os.environ, {update_list.RUN_TOKEN_ENV: ""}), \
                mock.patch.object(audio_info, "read", side_effect=AssertionError("read")):
            said = []
            self.assertEqual(update_list.rebuild_main(log=said.append), 0)
        text = "\n".join(said)
        self.assertIn("4 audio file(s) have no length and quality in the list yet", text)
        self.assertIn("update_list.py --read-audio-info", text)
        self.assertFalse(update_list.list_lock_holder(), "the lock is let go")

    def test_started_by_the_bot_it_reads(self):
        with mock.patch.dict(os.environ, BOT_RUN):
            said = []
            self.assertEqual(update_list.rebuild_main(log=said.append), 0)
        self.assertTrue(any("Audio info: done: 3 read" in line for line in said), said)

    def test_by_hand_the_reading_says_how_far_it_got(self):
        jobs, _result, _said = self.rebuild(read=False)
        with mock.patch.dict(os.environ, {update_list.RUN_TOKEN_ENV: ""}), \
                mock.patch.object(update_list, "AUDIO_PRINT_SECONDS", 0):
            self.set_config(LIST_AUDIO_INFO_THREADS=1)
            _result, out = self.quietly(update_list.run_audio_reading, jobs)
        self.assertIn("(Ctrl-C stops it; what was read is kept)", out)
        self.assertRegex(out, r"\[AUDIO-INFO\] Audio info: [1-4] of 4 read")


class AReadingWhoseBotIsGone(Case):

    def test_it_stops_by_itself(self):
        state = {"checked": 0.0, "gone": False}
        clock = [100.0]
        with mock.patch.dict(os.environ, {update_list.DAEMON_PID_ENV: "424242"}), \
                mock.patch.object(platform_compat, "pid_alive", lambda pid: False):
            self.assertTrue(update_list.reading_should_stop(clock=lambda: clock[0], _state=state))

    def test_its_bot_is_asked_only_now_and_then(self):
        state = {"checked": 0.0, "gone": False}
        clock = [100.0]
        asked = []
        with mock.patch.dict(os.environ, {update_list.DAEMON_PID_ENV: "424242"}), \
                mock.patch.object(platform_compat, "pid_alive", lambda pid: asked.append(pid) or True):
            for _ in range(5):
                self.assertFalse(update_list.reading_should_stop(clock=lambda: clock[0], _state=state))
            clock[0] += update_list.DAEMON_CHECK_SECONDS
            update_list.reading_should_stop(clock=lambda: clock[0], _state=state)
        self.assertEqual(asked, ["424242", "424242"])

    def test_a_run_by_hand_has_no_bot_to_ask(self):
        with mock.patch.dict(os.environ, {update_list.DAEMON_PID_ENV: ""}), \
                mock.patch.object(platform_compat, "pid_alive", side_effect=AssertionError("asked")):
            self.assertFalse(update_list.reading_should_stop(_state={"checked": 0.0, "gone": False}))

    def test_whether_a_process_is_alive(self):
        self.assertTrue(platform_compat.pid_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(platform_compat.pid_alive(child.pid))
        self.assertFalse(platform_compat.pid_alive(0))
        self.assertFalse(platform_compat.pid_alive("not a pid"))

    def test_the_next_start_stops_one_left_running(self):
        self.hold("reading")
        with redirect_stdout(io.StringIO()):
            self.assertTrue(commands.stop_orphaned_reading())
        self.assertTrue(update_list.stop_requested())

    def test_but_not_a_rebuild_and_not_when_there_is_none(self):
        self.assertFalse(commands.stop_orphaned_reading())
        self.hold("rebuild")
        self.assertFalse(commands.stop_orphaned_reading())
        self.assertFalse(update_list.stop_requested())

    def test_the_bot_start_asks(self):
        with io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            source = handle.read()
        startup = source[source.index("def startup("):]
        startup = startup[:startup.index("\ndef ")]
        self.assertIn("_commands_orphan.stop_orphaned_reading()", startup)


class TheConsoleSaysWhatHappened(first.Library):
    """Library > Read audio info, and `audioinfo` typed, answered "Reading
    ..." whatever happened; a refusal reached the window only through the
    debug feed (#1182 audit)."""

    def answer(self):
        import socket
        import tests.test_commands as commands_tests
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        session.authenticated = True
        sent = []
        session.send = sent.append
        with mock.patch.object(threading, "Thread", commands_tests._SyncThread), \
                mock.patch("announce.send_debug", lambda *a, **k: None):
            adminchat._cmd_audioinfo(session, "")
        return sent

    def test_refused_while_a_rebuild_runs(self):
        self.set_config(update_inprogress=True)
        self.assertEqual(self.answer(), ["Audio info: a list rebuild is running - it reads the new "
                                         "audio files itself once it has published."])

    def test_off(self):
        self.set_config(LIST_SHOW_AUDIO_INFO=False)
        (line,) = self.answer()
        self.assertIn("length and quality are off", line)

    def test_already_reading_with_how_far(self):
        commands.start_audio_watch(mock.Mock(pid=1), "rebuild", start=lambda: None)
        update_list.write_progress("reading", folder_index=3200, folder_count=12000, rate=230, force=True)
        self.assertEqual(self.answer(), ["Audio info: already reading - 3,200 of 12,000 read, 230/s."])

    def test_started(self):
        with mock.patch("subprocess.Popen", lambda *a, **k: mock.Mock(pid=2)), \
                mock.patch.object(commands, "start_audio_watch", lambda *a, **k: None):
            (line,) = self.answer()
        self.assertTrue(line.startswith("Audio info: Op started reading"), line)


class AnOlderScriptIsNeverSentReading(first.Library):
    """While the rebuild flag is still up but the progress file is already
    the reading's - the hand-over tick, or a rebuild stopping a reading - a
    1.9-1.11 script was sent "reading 37 12000 0" in the folder fields
    (#1182 audit)."""

    def setUp(self):
        super().setUp()
        self.set_config(update_inprogress=True)

    def test_an_older_script_gets_a_phase_it_draws(self):
        for phase in ("reading", "finding", "rewriting"):
            update_list.write_progress(phase, folder_index=37, folder_count=12000, rate=210, force=True)
            (line,) = adminchat.rebuild_lines(reading=False)
            self.assertTrue(line.startswith("DCCORE REBUILD publishing 0 0 0 "), (phase, line))

    def test_a_new_one_gets_the_reading_with_its_rate(self):
        update_list.write_progress("reading", folder_index=37, folder_count=12000, rate=210, force=True)
        (line,) = adminchat.rebuild_lines(reading=True)
        self.assertTrue(line.startswith("DCCORE REBUILD reading 37 12000 210 "), line)

    def test_a_rebuilds_own_phases_are_untouched(self):
        update_list.write_progress("scanning", folder_index=2, folder_count=5, files=900, force=True)
        for reading in (False, True):
            (line,) = adminchat.rebuild_lines(reading=reading)
            self.assertTrue(line.startswith("DCCORE REBUILD scanning 2 5 900 "), line)


class FindingIsNotWriting(first.Library):
    """#1189's observation 3: while a reading started alone reads the list to
    find what to read, the @DCCore panel said "writing the list"."""

    def setUp(self):
        super().setUp()
        commands.start_audio_watch(mock.Mock(pid=1), "on demand", start=lambda: None)

    def test_the_rebuild_line(self):
        update_list.clear_progress()
        self.assertTrue(adminchat.rebuild_lines(reading=True)[0].startswith("DCCORE REBUILD finding 0 0 0 "))
        update_list.write_progress(update_list.FINDING_PHASE, force=True)
        self.assertTrue(adminchat.rebuild_lines(reading=True)[0].startswith("DCCORE REBUILD finding "))
        for phase in ("rewriting", "packing", "publishing"):
            update_list.write_progress(phase, force=True)
            self.assertTrue(adminchat.rebuild_lines(reading=True)[0].startswith("DCCORE REBUILD rewriting "),
                            phase)

    def test_the_reading_only_run_says_finding_first(self):
        self.rebuild(read=False)
        phases = []
        real = update_list.write_progress
        with mock.patch.object(update_list, "write_progress",
                               lambda phase, **kw: phases.append(phase) or real(phase, **kw)), \
                redirect_stdout(io.StringIO()):
            update_list.run_audio_reading()
        self.assertEqual(phases[0], "finding")

    def test_the_title_and_the_dashboard(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"),
                     encoding="utf-8", newline="") as handle:
            mrc = handle.read().replace("\r\n", "\n")
        short = mrc[mrc.index("alias dccore.rebuild.short {"):]
        self.assertIn("if ($gettok(%l,1,32) == finding) { return audio info: finding what to read }",
                      short[:short.index("\n}")])
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            app = handle.read()
        self.assertIn('showAudioInfoStatus(writing ? t("tools.audioWriting") : t("tools.audioFinding"), false);', app)


if __name__ == "__main__":
    unittest.main()
