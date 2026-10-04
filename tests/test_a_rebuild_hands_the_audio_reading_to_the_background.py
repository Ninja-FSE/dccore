"""The daemon's side of the background audio reading (#1182).

A rebuild with LIST_SHOW_AUDIO_INFO on now publishes first, and the same
update_list.py process goes on reading audio lengths. If the daemon went on
waiting for it, !update would report nothing until the reading ended - hours
on a first run - and every later !update would be refused as "already
running". So the daemon stops waiting the moment THIS child reports the
"reading" phase: the rebuild is announced as done, the flag is released, and a
thread of its own watches the reading to the end.

What is pinned here:
- the hand-over: only on the child's own report, and the child is not killed;
- a rebuild started during a reading stops it first, then runs;
- a reading-only run is refused during a rebuild, answered with the progress
  while one runs, and refused when the setting is off;
- the watcher's lines: the start, the progress, the end, a stall;
- searches pause for the reading only while it swaps the list;
- the console's `audioinfo`, the @DCCore window's REBUILD lines, the
  dashboard's route and status, the dccore.mrc menu entry and panel;
- a settings.conf that still sets LIST_AUDIO_INFO_MINUTES loads cleanly.
"""

import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import runtime  # noqa: E402
import settings_file  # noqa: E402
import update_list  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, parse_source  # noqa: E402
# Modules, not classes: a TestCase imported by name runs again here.
import tests.test_a_working_rebuild_is_not_hung as not_hung  # noqa: E402
import tests.test_commands as commands_tests  # noqa: E402


class Child(not_hung.FakeChild):
    """A Popen stand-in with a pid, which the hand-over checks."""

    def __init__(self, pid=424242, **kwargs):
        super().__init__(**kwargs)
        self.pid = pid


# The token this bot gives its child (commands.audio_run_env()).
TOKEN = "token-of-this-run"


def result_line(**result):
    return update_list.AUDIO_RESULT_TAG + json.dumps(result)


class Case(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.progress = os.path.join(self.make_temp_dir(), "progress.json")
        self.set_config(LIST_PROGRESS_FILE=self.progress, LIST_SHOW_AUDIO_INFO=True)
        self.said = []
        real = announce.send_debug
        announce.send_debug = lambda text, category="INFO", notice=None: self.said.append((text, notice))
        self.addCleanup(setattr, announce, "send_debug", real)

    def report(self, phase="reading", token=TOKEN, at=None, **fields):
        payload = dict({"phase": phase, "pid": 424242, "token": token,
                        "at": time.time() if at is None else at,
                        "started_at": time.time() - 30}, **fields)
        with io.open(self.progress, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def lines(self):
        return [text for text, _notice in self.said]


class TheHandOver(Case):

    def test_only_this_childs_own_report_counts(self):
        """By the token it was given, not by its pid (#1182 audit): a
        Windows venv's python.exe runs the script as a second process."""
        self.assertFalse(commands.child_is_reading(TOKEN), "no progress file")
        self.report(phase="scanning")
        self.assertFalse(commands.child_is_reading(TOKEN))
        self.report(phase="reading", token="another-run")
        self.assertFalse(commands.child_is_reading(TOKEN), "a stopped reading's leftover")
        self.report(phase="reading", token="")
        self.assertFalse(commands.child_is_reading(""), "a run started by hand has none")
        self.report(phase="reading")
        self.assertTrue(commands.child_is_reading(TOKEN))

    def test_the_pid_does_not_matter(self):
        self.report(phase="reading", token=TOKEN)
        with io.open(self.progress, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["pid"] = 1
        with io.open(self.progress, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        self.assertTrue(commands.child_is_reading(TOKEN))

    def test_the_child_is_given_the_token_and_the_bots_pid(self):
        token, env = commands.audio_run_env()
        self.assertGreaterEqual(len(token), 16)
        self.assertEqual(env[update_list.RUN_TOKEN_ENV], token)
        self.assertEqual(env[update_list.DAEMON_PID_ENV], str(os.getpid()))
        self.assertNotEqual(commands.audio_run_env()[0], token, "one per run")

    def test_the_child_writes_it_into_its_progress(self):
        with mock.patch.dict(os.environ, {update_list.RUN_TOKEN_ENV: TOKEN}):
            update_list.write_progress("reading", force=True)
        self.assertEqual(update_list.read_progress()["token"], TOKEN)
        self.assertTrue(commands.child_is_reading(TOKEN))

    def test_the_watcher_lets_go_of_a_child_still_running(self):
        child = Child(ticks=None)
        subprocess_popen = mock.patch.object(subprocess, "Popen", lambda *a, **k: child)
        with subprocess_popen:
            asked = []
            result = commands.run_watching_for_a_stall(
                ["x"], tick=0.01, hand_over=lambda process: asked.append(process) or len(asked) >= 2)
        self.assertIsNone(result.returncode)
        self.assertIs(result.process, child)
        self.assertFalse(child.killed, "handed over, not stopped")
        self.assertEqual(len(asked), 2)

    def test_without_it_the_watcher_waits_as_before(self):
        child = Child(ticks=2, returncode=0, stdout="done")
        with mock.patch.object(subprocess, "Popen", lambda *a, **k: child):
            result = commands.run_watching_for_a_stall(["x"], tick=0.01)
        self.assertEqual((result.returncode, result.stdout), (0, "done"))


class ARebuild(Case):
    """handle_list_update_request() with its thread run inline and the child
    replaced, the way tests/test_commands.py drives it."""

    def setUp(self):
        super().setUp()
        real_thread = threading.Thread
        threading.Thread = commands_tests._SyncThread
        self.addCleanup(setattr, threading, "Thread", real_thread)
        real_sleep = time.sleep
        time.sleep = lambda *_a, **_k: None
        self.addCleanup(setattr, time, "sleep", real_sleep)
        self.watched = []
        real_watch = commands.start_audio_watch
        self.addCleanup(setattr, commands, "start_audio_watch", real_watch)
        commands.start_audio_watch = lambda process, kind, start=None, token="": (
            self.watched.append((process, kind, config.update_inprogress, token)),
            real_watch(process, kind, start=lambda: None, token=token))[1]

    def run_with(self, runner):
        real = commands.run_watching_for_a_stall
        commands.run_watching_for_a_stall = runner
        self.addCleanup(setattr, commands, "run_watching_for_a_stall", real)
        commands.handle_list_update_request("admin", "#chan", authorised=True)

    def test_handed_over_it_is_a_finished_rebuild_and_a_watched_reading(self):
        child = Child()

        given = {}

        def runner(argv, **kwargs):
            given["token"] = kwargs["env"][update_list.RUN_TOKEN_ENV]
            self.report(phase="reading", token=given["token"])
            self.assertTrue(kwargs["hand_over"](child), "its own token hands it over")
            self.report(phase="reading", token="another-run")
            self.assertFalse(kwargs["hand_over"](child), "another run's does not")
            handed = subprocess.CompletedProcess(argv, None, "", "")
            handed.process = child
            return handed

        self.run_with(runner)
        self.assertEqual(self.watched, [(child, "rebuild", True, given["token"])],
                         "watched while the rebuild flag was still up: never a gap")
        self.assertIs(runtime.audio_reading.process, child)
        self.assertFalse(config.update_inprogress, "the rebuild is over")
        self.assertTrue(config.last_list_update_ok)
        self.assertTrue(any("List update successfully completed" in line for line in self.lines()))

    def test_a_reading_already_over_says_how_it_went(self):
        out = "--- The list was updated successfully. ---\n" + result_line(
            outcome="nothing", read=0, unreadable=0, total=0, updated=[])
        self.run_with(lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, out, ""))
        self.assertEqual(self.watched, [])
        self.assertEqual(self.lines()[-1], "Audio info: nothing new to read.")
        self.assertEqual(runtime.audio_reading_last["outcome"], "nothing")

    def test_a_rebuild_during_a_reading_stops_it_first(self):
        """Stopped through the stop file, waited for, and the file cleared
        before the new child starts - or the new child's own reading would
        stop at its first read."""
        old = Child(pid=7)
        job = commands.start_audio_watch(old, "on demand", start=lambda: None)

        def obey():
            deadline = time.monotonic() + 10
            while not update_list.stop_requested() and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            with runtime.audio_reading_lock:
                runtime.audio_reading = None
            job.done.set()

        # The reading obeys on a real thread: threading.Thread is the inline
        # stand-in in this class.
        obeyer = _RealThread(target=obey, daemon=True)
        obeyer.start()

        seen = {}

        def runner(argv, **kwargs):
            seen["stopping"] = job.stopping
            seen["done"] = job.done.is_set()
            seen["stop file"] = update_list.stop_requested()
            seen["reading"] = runtime.audio_reading
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.run_with(runner)
        obeyer.join(10)
        self.assertEqual(seen, {"stopping": True, "done": True, "stop file": False, "reading": None})
        self.assertFalse(old.killed, "it stopped by itself, so nothing was killed")

    def test_a_stale_stop_request_is_cleared_with_no_reading_running(self):
        """Left by a run that was killed: the new child's reading would
        otherwise stop at its first read."""
        update_list.request_stop()
        seen = []
        self.run_with(lambda argv, **kwargs: seen.append(update_list.stop_requested())
                      or subprocess.CompletedProcess(argv, 0, "", ""))
        self.assertEqual(seen, [False])

    def test_one_that_does_not_stop_is_killed(self):
        old = Child(pid=7)
        commands.start_audio_watch(old, "rebuild", start=lambda: None)
        with mock.patch.object(commands, "AUDIO_STOP_WAIT", 0.01):
            real_wait = threading.Event.wait

            def short_wait(event, timeout=None):
                return real_wait(event, min(timeout or 0.01, 0.01))

            with mock.patch.object(threading.Event, "wait", short_wait):
                self.run_with(lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""))
        self.assertTrue(old.killed)


_RealThread = threading.Thread


class AReadingOnlyRun(Case):

    def setUp(self):
        super().setUp()
        self.started = []

        def popen(argv, **kwargs):
            self.started.append(argv)
            return Child(ticks=None)

        self.popen = popen
        real_watch = commands.start_audio_watch
        self.addCleanup(setattr, commands, "start_audio_watch", real_watch)
        commands.start_audio_watch = lambda process, kind, start=None, token="": real_watch(
            process, kind, start=lambda: None, token=token)

    def ask(self):
        return commands.handle_audio_info_request("admin", "#chan", authorised=True, popen=self.popen)

    def test_it_starts_the_reading_only_child(self):
        status, _message = self.ask()
        self.assertEqual(status, "started")
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0][-2:], [os.path.join(REPO_ROOT, "update_list.py"), "--read-audio-info"])
        self.assertEqual(runtime.audio_reading.kind, "on demand")

    def test_it_is_refused_while_a_rebuild_runs(self):
        self.set_config(update_inprogress=True)
        status, message = self.ask()
        self.assertEqual(status, "rebuilding")
        self.assertEqual(self.started, [])
        self.assertIn("it reads the new audio files itself once it has published", message)
        self.assertIn(message, self.lines(), "said where the console hears it")

    def test_one_already_running_is_answered_with_its_progress(self):
        self.ask()
        self.report(phase="reading", folder_index=3200, folder_count=12000, rate=230)
        status, message = self.ask()
        self.assertEqual(status, "running")
        self.assertEqual(len(self.started), 1, "not a second one")
        self.assertEqual(message, "Audio info: already reading - 3,200 of 12,000 read, 230/s.")

    def test_off_nothing_runs(self):
        self.set_config(LIST_SHOW_AUDIO_INFO=False)
        status, message = self.ask()
        self.assertEqual((status, self.started), ("off", []))
        self.assertIn("length and quality are off", message)

    def test_a_stale_stop_request_is_cleared_first(self):
        update_list.request_stop()
        self.ask()
        self.assertFalse(update_list.stop_requested())

    def test_only_an_admin_may_ask(self):
        status, _message = commands.handle_audio_info_request("stranger", "#chan", popen=self.popen)
        self.assertEqual((status, self.started), ("denied", []))


class TheWatcher(Case):

    def watch(self, child, **kwargs):
        job = commands.start_audio_watch(child, "rebuild", start=lambda: None, token=TOKEN)
        commands.watch_audio_reading(job, tick=0.01, **kwargs)
        return job

    def test_the_end_is_said_and_kept_for_the_dashboard(self):
        child = Child(ticks=0, returncode=0, stdout=result_line(
            outcome="done", read=12000, unreadable=20, total=12000, updated=["Main"]))
        job = self.watch(child)
        self.assertTrue(job.done.is_set())
        self.assertIsNone(runtime.audio_reading)
        self.assertEqual(self.said[-1], ("Audio info: done: 11,980 read, 20 unreadable, list updated.", None))
        self.assertEqual(runtime.audio_reading_last["outcome"], "done")
        self.assertEqual(runtime.audio_reading_last["message"], self.said[-1][0])

    def test_the_start_once_and_the_progress_on_the_console(self):
        self.report(phase="reading", folder_index=3200, folder_count=12000, rate=230)
        child = Child(ticks=3, returncode=0, stdout=result_line(outcome="stopped", read=3200, total=12000))
        out = io.StringIO()
        with mock.patch.object(commands, "AUDIO_PROGRESS_SECONDS", 0), \
                mock.patch("sys.stdout", out):
            self.watch(child)
        self.assertEqual(self.lines().count("Audio info: reading 12,000 files in the background."), 1)
        self.assertIn("[AUDIO-INFO] Audio info: 3,200 of 12,000 read, 230/s.", out.getvalue())
        self.assertEqual(self.lines()[-1], "Audio info: stopped: 3,200 of 12,000 read and kept; "
                                           "the rest are read next time.")

    def test_a_silent_reading_is_a_stall(self):
        self.set_config(LIST_UPDATE_STALL_SECONDS=60)
        self.report(phase="reading", at=time.time() - 3600, folder_count=5)
        child = Child(ticks=None)
        self.watch(child)
        self.assertTrue(child.killed)
        text, notice = self.said[-1]
        self.assertTrue(text.startswith("Audio info: stopped - nothing reported for"), text)
        self.assertEqual(notice, "error")
        self.assertEqual(runtime.audio_reading_last["outcome"], "stalled")
        self.assertFalse(os.path.exists(self.progress), "its leftover would read as running")

    def test_a_reading_killed_on_request_is_stopped_not_failed(self):
        child = Child(ticks=0, returncode=1, stdout="")
        job = commands.start_audio_watch(child, "rebuild", start=lambda: None)
        job.stopping = True
        commands.watch_audio_reading(job, tick=0.01)
        self.assertEqual(runtime.audio_reading_last["outcome"], "stopped")
        self.assertIsNone(self.said[-1][1])

    def test_the_list_browsers_page_tables_are_dropped_when_it_ends(self):
        """A reading may publish the list again with its old mtime put back,
        and the List Browser's folder tables (#1128) are keyed on mtime and
        size: a rewrite that kept the size would be answered from a stale
        table. Dropped whatever the outcome."""
        import list as list_mod
        endings = {
            "done": Child(ticks=0, returncode=0, stdout=result_line(
                outcome="done", read=1, unreadable=0, total=1, updated=["Main"])),
            "failed": Child(ticks=0, returncode=1, stdout="", stderr="Traceback\nMemoryError"),
        }
        for outcome, child in endings.items():
            with self.subTest(outcome=outcome), \
                    mock.patch.object(list_mod, "forget_folder_tables") as forget:
                self.watch(child)
                forget.assert_called_once_with()

    def test_a_reading_that_dies_is_a_failure(self):
        child = Child(ticks=0, returncode=1, stdout="", stderr="Traceback\nMemoryError")
        self.watch(child)
        self.assertEqual(runtime.audio_reading_last["outcome"], "failed")
        self.assertEqual(self.said[-1], ("Audio info: failed: MemoryError.", "error"))


class SearchesWaitOnlyForTheSwap(Case):

    def setUp(self):
        super().setUp()
        self.set_config(PAUSE_ON_UPDATE=True, PAUSE_FOR_WHOLE_UPDATE=True, update_inprogress=False)

    def test_nothing_pauses_without_a_reading(self):
        update_list.write_progress("publishing", force=True)
        self.assertFalse(list_mod.rebuild_pauses_requests())

    def test_a_reading_pauses_only_while_it_packs_and_swaps(self):
        commands.start_audio_watch(Child(), "on demand", start=lambda: None)
        self.assertFalse(list_mod.rebuild_pauses_requests(), "no progress file yet")
        for phase, pauses in (("reading", False), ("rewriting", False),
                              ("packing", True), ("publishing", True)):
            update_list.write_progress(phase, force=True)
            self.assertEqual(list_mod.rebuild_pauses_requests(), pauses, phase)
        update_list.clear_progress()
        self.assertFalse(list_mod.rebuild_pauses_requests(), "ended, not yet noticed")


class TheConsoleAndTheWindow(Case):

    def session(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        session.authenticated = True
        return session

    def test_audioinfo_is_a_console_command(self):
        self.assertIn("audioinfo", adminchat.COMMANDS)
        session = self.session()
        sent = []
        session.send = sent.append
        asked = []
        with mock.patch.object(commands, "handle_audio_info_request",
                               lambda user, target, authorised=False: asked.append((user, authorised))
                               or ("started", "Audio info: Op started reading.")), \
                mock.patch.object(threading, "Thread", commands_tests._SyncThread):
            adminchat._cmd_audioinfo(session, "")
        self.assertEqual(asked, [("Op", True)])
        self.assertEqual(sent, ["Audio info: Op started reading."])

    def test_the_reading_is_a_rebuild_line_for_a_script_that_draws_it(self):
        commands.start_audio_watch(Child(), "rebuild", start=lambda: None)
        self.report(phase="reading", folder_index=3200, folder_count=12000, rate=230)
        self.assertEqual(adminchat.rebuild_lines(), [], "an older script is sent nothing")
        (line,) = adminchat.rebuild_lines(reading=True)
        self.assertRegex(line, r"^DCCORE REBUILD reading 3200 12000 230 (29|30|31)$")
        self.report(phase="publishing")
        (line,) = adminchat.rebuild_lines(reading=True)
        self.assertTrue(line.startswith("DCCORE REBUILD rewriting 0 0 0 "), line)

    def test_a_rebuild_still_reads_as_a_rebuild(self):
        self.set_config(update_inprogress=True)
        self.report(phase="scanning", folder_index=2, folder_count=5, files=900)
        (line,) = adminchat.rebuild_lines(reading=True)
        self.assertTrue(line.startswith("DCCORE REBUILD scanning 2 5 900 "), line)

    def test_the_version_gate(self):
        self.assertFalse(adminchat.script_draws_audio("1.11"))
        self.assertTrue(adminchat.script_draws_audio("1.12"))
        session = self.session()
        session.send = lambda text: None
        adminchat._cmd_hello(session, "dccore.mrc 1.12")
        self.assertTrue(session.draws_audio)
        adminchat._cmd_hello(session, "dccore.mrc 1.11")
        self.assertFalse(session.draws_audio)
        self.assertTrue(session.draws_rebuild, "1.11 still draws a rebuild")

    def test_the_session_asks_for_it_when_its_script_draws_it(self):
        """Both the status burst and the lines between bursts pass the
        session's own answer - a call, not a docstring that names it."""
        import ast
        with io.open(os.path.join(REPO_ROOT, "src", "adminchat.py"), encoding="utf-8") as handle:
            tree = parse_source(handle.read(), "adminchat.py")
        session = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == "Session")
        callers = set()
        for method in session.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            for call in ast.walk(method):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id in ("rebuild_lines", "status_lines")):
                    for keyword in call.keywords:
                        if (keyword.arg == "reading" and isinstance(keyword.value, ast.Attribute)
                                and keyword.value.attr == "draws_audio"):
                            callers.add((method.name, call.func.id))
        self.assertEqual(callers, {("send_status", "status_lines"),
                                   ("send_rebuild_progress", "rebuild_lines")})


class TheDashboard(Case):

    def test_the_status_carries_the_reading(self):
        payload = webserver.build_update_list_status_payload()
        self.assertEqual(payload["audio"], {"enabled": True, "running": False, "last": None})
        commands.start_audio_watch(Child(), "rebuild", start=lambda: None)
        self.report(phase="reading", folder_index=10, folder_count=40, rate=5)
        payload = webserver.build_update_list_status_payload()
        self.assertTrue(payload["audio"]["running"])
        self.assertEqual((payload["progress"]["phase"], payload["progress"]["rate"]), ("reading", 5))

    def test_the_button_maps_each_answer(self):
        answers = {"started": 200, "running": 200, "rebuilding": 409, "off": 409, "failed": 500}
        for status, code in answers.items():
            with mock.patch.object(commands, "handle_audio_info_request",
                                   lambda *a, **k: (status, "words")):
                got, payload = webserver.start_audio_info_reading()
            self.assertEqual(got, code, status)
            if code == 409:
                self.assertEqual(payload["reason"], status)


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class TheRoute(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        import tests.test_dashboard_routes as routes
        self.password = routes.PASSWORD
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(self.password, iterations=1000))
        self.app = webserver.create_app()
        self.client = self.app.test_client()
        webserver._web_bad_ips.clear()
        self.started = []
        real = webserver.start_audio_info_reading
        webserver.start_audio_info_reading = lambda: (self.started.append(True), (200, {"reading": "started"}))[1]
        self.addCleanup(setattr, webserver, "start_audio_info_reading", real)

    def test_a_logged_in_post_starts_it(self):
        self.client.post("/login", data={"password": self.password})
        resp = self.client.post("/api/tools/audio-info")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.started, [True])

    def test_post_only_and_behind_the_login(self):
        methods = set()
        for rule in self.app.url_map.iter_rules():
            if str(rule) == "/api/tools/audio-info":
                methods = set(rule.methods)
        self.assertIn("POST", methods)
        self.assertNotIn("GET", methods)
        self.assertEqual(self.client.post("/api/tools/audio-info").status_code, 401)
        self.assertEqual(self.started, [])


class ThePage(unittest.TestCase):

    def read(self, *parts):
        with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_the_button_posts_to_the_route(self):
        app = self.read("web", "app.js")
        self.assertIn('postJson("/api/tools/audio-info", {})', app)
        html = self.read("web", "index.html")
        for element in ('id="audio-info-run-btn"', 'id="audio-info-status"', 'id="audio-info-bar"',
                        'data-i18n="tools.readAudioInfo"', 'data-i18n="tools.readAudioInfoWhat"'):
            self.assertIn(element, html)

    def test_every_string_it_uses_is_in_every_language(self):
        app = self.read("web", "app.js")
        start = app.index("// ------------------------------------------------- Read audio info (#1182)")
        end = app.index("// The Tools view runs nothing on its own beyond the update above.")
        keys = set(re.findall(r't\("(tools\.\w+)"\)', app[start:end]))
        keys |= {"tools.readAudioInfo", "tools.readAudioInfoWhat", "tools.audioDone",
                 "tools.audioDoneUnchanged"}
        self.assertGreaterEqual(len(keys), 12, sorted(keys))
        for lang in ("en", "es", "fr"):
            strings = json.loads(self.read("web", "lang", lang + ".json"))
            for key in sorted(keys):
                with self.subTest(lang=lang, key=key):
                    self.assertIn(key, strings)
                    if lang != "en":
                        self.assertNotEqual(strings[key], json.loads(self.read("web", "lang", "en.json"))[key])

    def test_the_old_rebuild_phase_is_gone_from_the_update_card(self):
        self.assertNotIn('progress.phase === "audio"', self.read("web", "app.js"))


class TheMircScript(unittest.TestCase):

    def setUp(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"),
                     encoding="utf-8", newline="") as handle:
            self.text = handle.read().replace("\r\n", "\n")

    def test_library_has_read_audio_info(self):
        menu = self.text[self.text.index("menu @DCCore {"):]
        library = menu[menu.index("\n  Library\n"):]
        library = library[:library.index("\n  User control\n")]
        self.assertIn("\n  .Read audio info:dccore.send audioinfo", library)

    def test_the_version_is_a_new_feature(self):
        match = re.search(r"alias dccore\.ver \{ return ([0-9.]+) \}", self.text)
        self.assertEqual(match.group(1), adminchat.AUDIO_SCRIPT_VERSION)

    def test_the_panel_draws_the_reading(self):
        panel = self.text[self.text.index("alias dccore.panel {"):]
        panel = panel[:panel.index("\n}")]
        section = panel[panel.index("Audio info (#1182)"):panel.index("aline -l %head $dccore.win Rebuilding")]
        self.assertIn("if ($istok(reading rewriting finding,$gettok(%r,1,32),32)) {", section)
        self.assertIn("elseif ($gettok(%r,1,32) == finding) {", section)
        self.assertIn("finding what to read }", section)
        self.assertIn("aline -l %head $dccore.win Audio info", section)
        self.assertIn("read $dccore.dot $dccore.num($gettok(%r,4,32)) $+ /s", section)

    def test_the_title_bar_says_it(self):
        short = self.text[self.text.index("alias dccore.rebuild.short {"):]
        short = short[:short.index("\n}")]
        self.assertIn("if ($gettok(%l,1,32) == reading) { return audio info", short)


class AnOldSettingStillLoads(DCCoreTestCase):

    def test_list_audio_info_minutes_loads_without_a_word(self):
        path = os.path.join(self.make_temp_dir(), "settings.conf")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("[list]\nLIST_AUDIO_INFO_MINUTES = 30\n")
        namespace = dict(vars(config))
        said = []
        report = settings_file.apply_to(namespace, path=path, log=said.append)
        self.assertEqual(report["unknown"], [])
        self.assertEqual(report["bad"], [])
        self.assertEqual(said, ["[CONFIG] Applied 1 setting(s) from settings.conf."])

    def test_and_nothing_reads_it(self):
        """Ignored: no code outside defaults.py names it any more."""
        for name in ("update_list.py", os.path.join("src", "audio_info.py"),
                     os.path.join("src", "commands.py")):
            with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                code = handle.read()
            self.assertNotIn('getattr(config, "LIST_AUDIO_INFO_MINUTES"', code, name)


if __name__ == "__main__":
    unittest.main()
