"""What a review of the settings window's console commands found (#1264).

Each class is one finding, driven through the real functions - the real
Session and its writer thread, the real reader loop, the real saves:

  * a `served` snapshot within the dashboard's own limits overflowed the
    500-line outbox before the writer could drain it, so SRVBEGIN never
    arrived - snapshots now wait for room instead of dropping;
  * the reader decoded each 1024-byte chunk on its own, so a character
    split across two chunks was saved as two U+FFFD;
  * `nan` and `inf` were numbers to float(), and to the saves behind it;
  * str.split()/strip() take a no-break space as a separator, which the
    encoding does not escape, so such a value did not come back unchanged;
  * a commit read config outside the reload lock, inside a rehash's reload
    window;
  * a legal row with a 4096-character path was "too long" for the reader;
  * a typo in a subcommand logged the rest of the line - a password, say;
  * a lone `set` waiting for its confirmation silently swallowed the next
    lone `set`.
"""

import contextlib
import io
import os
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import commands  # noqa: E402
import console_settings  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import on_connect  # noqa: E402
import runtime  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_console_settings_commands import (  # noqa: E402
    SettingsCase, as_input, logged, make_session, read_bytes, rows, run)


def real_session(case, start_writer=True):
    """A Session on a socket pair, its own send() and (optionally) its
    writer thread - the outbox and all."""
    near, far = socket.socketpair()
    case.addCleanup(far.close)
    case.addCleanup(near.close)
    session = adminchat.Session(near, "192.0.2.1", "alfa", "alfa.example")
    session.authenticated = True
    session.structured = True
    case.addCleanup(session.close, None)
    if start_writer:
        session.start_writer()
    return session, far


def small_buffers(*socks):
    """Shrink a socket pair's buffers to a few KB, macOS-sized.

    A Unix socket pair on macOS buffers about 8 KB; Linux's and Windows'
    hold far more. A test that wrote a long row BEFORE starting the thread
    that reads it passed on those two and hung for ever on macOS (the CI
    shard was killed after 30 minutes). Small buffers make that mistake hang
    on a platform that honours them; Windows' loopback pair does not (tried),
    so the real protection is feed_while_reading() below, which starts the
    reader before it writes.
    """
    for sock in socks:
        for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
            try:
                sock.setsockopt(socket.SOL_SOCKET, option, 4096)
            except OSError:
                pass


def feed_while_reading(case, far, payload, session, seconds=10):
    """Run the real _reader_loop on `session` and write `payload` to the far
    end WHILE it reads, then close the far end's sending side.

    The reader starts first and the write happens on a thread of its own,
    so a payload bigger than the socket buffer drains as it is written
    rather than blocking on a reader that has not started. A reader that
    closes the session part way (Line too long) breaks the pipe: that is
    the case under test, not an error.
    """
    reader = threading.Thread(target=adminchat._reader_loop, args=(session,), daemon=True)
    reader.start()

    def write():
        try:
            far.sendall(payload)
            far.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    reader.join(seconds)
    case.assertFalse(reader.is_alive(), "the reader loop did not finish")
    writer.join(seconds)


def read_until(sock, done, limit=30.0):
    """Every line `sock` receives until done(lines) holds or `limit` passes."""
    sock.settimeout(0.5)
    data = b""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            chunk = None
        if chunk == b"":
            break
        if chunk:
            data += chunk
        lines = data.decode("utf-8", "replace").splitlines()
        if done(lines):
            return lines
    return data.decode("utf-8", "replace").splitlines()


def quietly(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


class ABigSnapshotArrivesWhole(DCCoreTestCase):
    """16 lists of 40 folders: a set the dashboard itself saves, and ~700
    lines - more than the outbox holds."""

    def setUp(self):
        super().setUp()
        root = self.make_temp_dir()
        lists = []
        for i in range(webserver.MAX_SERVED_LISTS):
            folders = []
            for j in range(40):
                path = os.path.join(root, f"l{i}f{j}")
                os.makedirs(path)
                folders.append(library.Folder(f"F{i}-{j}", path))
            lists.append(library.ServedList(f"List{i}", i == 0, [f"#music{i}"], folders, {}))
        library.save_lists(lists)
        self.expected = console_settings.served_lines(webserver.build_lists_payload())
        self.assertGreater(len(self.expected), adminchat.OUTBOX_MAX)

    def test_the_client_gets_begin_every_row_and_a_matching_end(self):
        session, far = real_session(self)
        got = []
        reader = threading.Thread(target=lambda: got.extend(read_until(
            far, lambda lines: any(l.startswith("DCCORE SRVEND") for l in lines))))
        reader.start()
        quietly(adminchat.handle_command, session, "served")
        reader.join(60)
        self.assertEqual([l for l in got if l.startswith("DCCORE SRV")], self.expected)
        self.assertEqual(session.dropped, 0)

    def test_a_client_that_stops_reading_does_not_hold_the_console_for_ever(self):
        self.addCleanup(setattr, adminchat, "BULK_WAIT", adminchat.BULK_WAIT)
        adminchat.BULK_WAIT = 0.2
        session, _far = real_session(self, start_writer=False)   # nothing drains: a stalled client
        started = time.monotonic()
        quietly(adminchat.handle_command, session, "served")
        self.assertLess(time.monotonic() - started, 10)
        queued = list(session._outbox)
        self.assertEqual(queued[0], self.expected[0])
        self.assertFalse(any(l.startswith("DCCORE SRVEND") for l in queued))
        self.assertTrue(queued[-1].startswith("DCCORE OUT The rest of that list was not sent"), queued[-1])
        self.assertEqual(session.dropped, 0)
        self.assertLessEqual(len(queued), adminchat.OUTBOX_MAX - adminchat.BULK_HEADROOM + 1)


class EverySnapshotIsSentWithoutDropping(DCCoreTestCase):
    """The same back-pressure for every framed dump, and for the prose a
    person gets: a hand-written library_folders.json has no cap at all."""

    def test_each_dump_goes_through_send_lines(self):
        for structured in (True, False):
            for command in ("settings", "served", "folders", "onconnect", "banlist"):
                session = make_session(self, structured=structured)
                bulk = []
                session.send_lines = lambda lines, bulk=bulk: bulk.append(list(lines))
                run(session, command)
                with self.subTest(command=command, structured=structured):
                    self.assertEqual(len(bulk), 1)
                    self.assertEqual(session.sent, [])


class ACharacterSplitAcrossTwoReadsSurvives(DCCoreTestCase):

    def test_through_the_real_reader_loop(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        small_buffers(near, far)
        session = adminchat.Session(near, "192.0.2.1", "alfa", "alfa.example")
        session.authenticated = True
        session.send = lambda *a, **k: None
        seen = []
        real = adminchat.handle_command
        adminchat.handle_command = lambda s, line: seen.append(line)
        self.addCleanup(setattr, adminchat, "handle_command", real)

        row = "folders row 2 Bjork D:/Music/Bj\u00f6rk"
        head = len(row.encode("utf-8")) - len("\u00f6rk".encode("utf-8"))
        filler = "x" * (1024 - head - 1 - 1) + "\n"     # the o-umlaut's two bytes straddle 1024
        payload = (filler + row + "\n").encode("utf-8")
        self.assertEqual(payload.index("\u00f6".encode("utf-8")), 1023)
        feed_while_reading(self, far, payload, session)
        self.assertEqual([l for l in seen if l.startswith("folders row 2")], [row])


class TheRehashNamesWhoSaved(DCCoreTestCase):
    """A save from the console is logged as the console's, not the dashboard's.

    The console saves through the dashboard's own apply_settings_changes(),
    and that used to start its rehash as "WEB-DASHBOARD" whoever asked: the
    first save from the mIRC settings window was logged "Rehash triggered by
    WEB-DASHBOARD from WEB-DASHBOARD".
    """

    def setUp(self):
        super().setUp()
        self.started = threading.Event()
        self.args = []

        def record(*args, **kwargs):
            self.args.append(args)
            self.started.set()
        real = commands.handle_rehash_request
        commands.handle_rehash_request = record
        self.addCleanup(setattr, commands, "handle_rehash_request", real)
        real_save = settings_file.save
        settings_file.save = lambda namespace, changes: {"written": sorted(changes), "unchanged": []}
        self.addCleanup(setattr, settings_file, "save", real_save)

    def rehash_source(self):
        self.assertTrue(self.started.wait(10), "no rehash was started")
        return self.args[-1]

    def test_a_console_commit_is_the_console_s(self):
        session = make_session(self)
        run(session, "setbegin")
        run(session, "set MAX_USER_QUEUE " + str(int(config.MAX_USER_QUEUE) + 1))
        run(session, "setcommit")
        self.assertEqual(self.rehash_source(), (session.nick, adminchat.CONSOLE_SOURCE))

    def test_the_dashboard_is_still_the_dashboard_s(self):
        status, _result = webserver.apply_settings_changes(
            {"MAX_USER_QUEUE": str(int(config.MAX_USER_QUEUE) + 1)})
        self.assertEqual(status, 200)
        self.assertEqual(self.rehash_source(),
                         (webserver.WEB_DASHBOARD_SOURCE, webserver.WEB_DASHBOARD_SOURCE))


class NotANumber(SettingsCase):

    def test_onconnect_delay_refuses_nan_and_inf(self):
        on_connect.save(["MODE %nick% +x"], 3)
        before = read_bytes(on_connect.on_connect_file())
        for word in ("nan", "inf", "-inf", "NaN"):
            run(self.session, "onconnect begin")
            reply = run(self.session, f"onconnect delay {word}")
            with self.subTest(word=word):
                self.assertTrue(reply and reply[0].startswith("DCCORE OCERR "), reply)
            run(self.session, "onconnect line 1 MODE %nick% +x")
            run(self.session, "onconnect commit")
        self.assertEqual(read_bytes(on_connect.on_connect_file()), before)

    def test_the_dashboards_on_connect_save_refuses_them_too(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertTrue(on_connect.problems(["MODE %nick% +x"], value))

    def test_a_float_setting_refuses_them(self):
        for word in ("nan", "inf", "-inf"):
            run(self.session, "setbegin")
            reply = run(self.session, f"set MSG_DELAY {word}")
            with self.subTest(word=word):
                self.assertTrue(reply and reply[0].startswith("DCCORE SETERR MSG_DELAY "), reply)
                with self.assertRaises(ValueError):
                    settings_file.coerce("MSG_DELAY", word, 5.0, float)
            run(self.session, "setabort")
        self.assertEqual(self.apply.calls, [])


NBSP, IDEOGRAPHIC = "\u00a0", "\u3000"


class UnicodeWhitespaceIsPartOfTheValue(SettingsCase):

    def test_a_setting_ending_in_one_comes_back_unchanged(self):
        self.set_config(PRIVATE_MESSAGE_DECLINE_TEXT=f"No{NBSP}thanks{IDEOGRAPHIC}")
        (row,) = [r for r in rows(run(self.session, "settings"), "SETF")
                  if r.startswith("PRIVATE_MESSAGE_DECLINE_TEXT ")]
        value = row.split(" ", 2)[2]
        self.assertTrue(value.endswith(IDEOGRAPHIC))
        run(self.session, "setbegin")
        self.assertEqual(run(self.session, f"set PRIVATE_MESSAGE_DECLINE_TEXT {value}"), [])
        self.assertEqual(run(self.session, "setcommit"),
                         ["DCCORE SETDONE ok 0 1 - Nothing changed; nothing was saved."])

    def test_served_and_folders_labels_and_channels(self):
        root = self.make_temp_dir()
        music = os.path.join(root, f"My{NBSP}Music")
        os.makedirs(music)
        library.save_folders([library.Folder(f"My{NBSP}Music{IDEOGRAPHIC}", music)])
        library.save_lists([library.ServedList(f"Main{NBSP}List", True, [f"#music{NBSP}x"],
                                               [library.Folder(f"{IDEOGRAPHIC}Label", music)], {})])
        for command, txn in (("served", "served_txn"), ("folders", "folders_txn")):
            lines = run(self.session, command)
            run(self.session, f"{command} begin")
            for line in as_input(lines):
                with self.subTest(line=line):
                    self.assertEqual(run(self.session, line), [])
            payload = (webserver.build_lists_payload()["lists"] if command == "served"
                       else webserver.build_folders_payload()["folders"])
            with self.subTest(command=command):
                got = getattr(self.session, txn)
                self.assertEqual(got.lists if command == "served" else got.folders, payload)
                self.assertEqual(run(self.session, f"{command} commit")[0].split()[2], "unchanged")

    def test_an_onconnect_line(self):
        command = f"PRIVMSG X@channels.example :LOGIN alfa pass{NBSP}word{IDEOGRAPHIC}x"
        on_connect.save([command], 2)
        lines = run(self.session, "onconnect")
        run(self.session, "onconnect begin")
        for line in as_input(lines):
            run(self.session, line)
        self.assertEqual(self.session.onconnect_txn.commands, [command])


class ACommitReadsConfigUnderTheReloadLock(SettingsCase):
    """Inside a rehash's reload window config holds defaults.py's literals
    for a moment. A `set` back to the shipped value, committed then, was
    judged against that and dropped as unchanged."""

    def test_a_commit_waits_for_the_reload_to_finish(self):
        name = "MAX_USER_QUEUE"
        default = getattr(config, name)
        self.set_config(**{name: default + 5})              # what settings.conf says
        run(self.session, "setbegin")
        run(self.session, f"set {name} {default}")          # the operator puts it back
        holding, done = threading.Event(), threading.Event()

        def reload():
            with runtime.config_reload_lock:
                setattr(config, name, default)              # the literal, for a moment
                holding.set()
                done.wait(1.0)                              # long enough to be caught inside
                setattr(config, name, default + 5)          # settings.conf applied again
        thread = threading.Thread(target=reload)
        thread.start()
        self.assertTrue(holding.wait(5))
        reply = run(self.session, "setcommit")
        done.set()
        thread.join(5)
        self.assertEqual(self.apply.calls, [{name: str(default)}])
        self.assertTrue(reply[0].startswith("DCCORE SETDONE ok 1 0 "), reply)


class ALongLegalRowIsNotTooLong(DCCoreTestCase):

    def reader(self, payload, authenticated=True):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        small_buffers(near, far)
        session = adminchat.Session(near, "192.0.2.1", "alfa", "alfa.example")
        session.authenticated = authenticated
        closed = []
        real_close = session.close
        session.close = lambda announce_text="Session closed.": (closed.append(announce_text),
                                                                  real_close(announce_text=None))
        session.send = lambda *a, **k: None
        seen = []
        real = adminchat.handle_command
        adminchat.handle_command = lambda s, line: seen.append(line)
        self.addCleanup(setattr, adminchat, "handle_command", real)
        feed_while_reading(self, far, payload.encode("utf-8"), session)
        return seen, closed

    def test_a_folder_row_with_the_longest_label_and_path_goes_through(self):
        row = ("served folder 1 " + console_settings.encode_token("L" * webserver.MAX_FOLDER_PATH_LEN)
               + " " + "/" + "p" * (webserver.MAX_FOLDER_PATH_LEN - 1))
        seen, closed = self.reader(row + "\n")
        self.assertEqual(seen, [row])
        self.assertNotIn("Line too long.", closed)

    def test_past_the_limit_it_still_closes(self):
        _seen, closed = self.reader("x" * (adminchat.LINE_MAX + 10))
        self.assertIn("Line too long.", closed)

    def test_before_the_password_the_old_limit_stands(self):
        _seen, closed = self.reader("x" * (adminchat.PASSWORD_LINE_MAX + 10), authenticated=False)
        self.assertIn("Line too long.", closed)


class OnlyTheCommandWordIsLogged(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.session = make_session(self)

    def test_a_typo_in_a_subcommand_logs_nothing_after_it(self):
        out = logged(self.session, "onconnect lines 1 PRIVMSG X :LOGIN alfa sekrit")
        self.assertIn("ran: onconnect\n", out)
        self.assertNotIn("sekrit", out)

    def test_each_family_member_logs_its_word_and_a_real_subcommand(self):
        for line, expected in (("settings nickname", "ran: settings\n"), ("served begin", "ran: served begin\n"),
                               ("setcommit confirm", "ran: setcommit confirm\n"),
                               ("folders abort extra words", "ran: folders abort\n"),
                               ("onconnect resend", "ran: onconnect resend\n")):
            with self.subTest(line=line):
                self.assertTrue(logged(self.session, line).endswith(expected))

    def test_rows_are_not_logged_at_all(self):
        self.session.settings_txn = console_settings.SettingsTransaction([])   # buffered, not saved
        for line in ("set NICKNAME alfa", "served list 1 1 Main", "served chan 1 #music normal",
                     "served folder 1 a /music", "folders row 1 a /music", "onconnect line 1 MODE x",
                     "onconnect delay 2"):
            with self.subTest(line=line):
                self.assertEqual(logged(self.session, line), "")

    def test_other_commands_are_logged_as_before(self):
        self.assertIn("ran: uptime", logged(self.session, "uptime"))


class AConfirmationALoneSetLeftOpenEndsCleanly(SettingsCase):

    def setUp(self):
        super().setUp()
        self.set_config(DEBUG_CHANNEL="#music-debug", MAX_DCC_SLOTS=2)

    def test_the_next_lone_set_ends_it_and_saves_at_once(self):
        self.assertTrue(run(self.session, "set DEBUG_CHANNEL")[0].startswith("DCCORE SETDONE confirm "))
        reply = run(self.session, "set MAX_DCC_SLOTS 3")
        self.assertEqual(reply[0], "DCCORE SETDONE aborted 1")
        self.assertTrue(reply[1].startswith("DCCORE SETDONE ok 1 0 "), reply)
        self.assertEqual(self.apply.calls, [{"MAX_DCC_SLOTS": "3"}])

    def test_any_other_command_ends_it_too(self):
        run(self.session, "set DEBUG_CHANNEL")
        reply = run(self.session, "consolecaps")
        self.assertEqual(reply[0], "DCCORE SETDONE aborted 1")
        self.assertIsNone(self.session.settings_txn)

    def test_setcommit_confirm_and_setabort_still_answer_it(self):
        run(self.session, "set DEBUG_CHANNEL")
        run(self.session, "setcommit confirm")
        self.assertEqual(self.apply.calls, [{"DEBUG_CHANNEL": "", "confirm_debug_channel_removed": True}])
        run(self.session, "set DEBUG_CHANNEL")
        self.assertEqual(run(self.session, "setabort"), ["DCCORE SETDONE aborted 1"])

    def test_an_explicit_transaction_waiting_for_confirm_is_left_open(self):
        run(self.session, "setbegin")
        run(self.session, "set DEBUG_CHANNEL")
        run(self.session, "setcommit")
        run(self.session, "consolecaps")
        run(self.session, "set MAX_DCC_SLOTS 3")
        self.assertIsNotNone(self.session.settings_txn)
        self.assertEqual(self.apply.calls, [])


if __name__ == "__main__":
    unittest.main()
