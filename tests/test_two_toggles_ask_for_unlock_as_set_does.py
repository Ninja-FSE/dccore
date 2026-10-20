"""`checkupdates on|off` and `consolefeed on|off` ask for unlock, as `set` does.

Each of them, given an argument, saves a setting - CHECK_FOR_UPDATES or
DEBUG_TO_CONSOLE - through settings_file.save() and a rehash. A console that
logged in with a paired token changes settings only after `unlock
<password>` once in that session (#1264), so these two answer it the same
`DCCORE LOCKED <command> <sentence>` line until then. With no argument they
only report the setting and stay open; a bad argument still gets its usage
line; a password login, a session that unlocked, and the dashboard's own
Console save as before; a read-only token is refused both with an argument
before the handler runs, as it was.

dccore.mrc says a `LOCKED checkupdates` or `LOCKED consolefeed` line in
@DCCore, ahead of the settings window's routing: the window never asked for
it. dccore.mrc cannot run here, so its dispatcher is evaluated from its own
conditions by tests/test_a_failed_rehash_is_not_said_to_be_in_effect.py's
handling(), on the very lines the bot sends. With the settings window open,
a refused `consolefeed` also puts the window's console feed box back as the
bot has it (dccore.sw.locked.toggle), run here statement by statement.
"""

import contextlib
import io
import itertools
import os
import re
import socket
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (REPO_ROOT, os.path.join(REPO_ROOT, "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import adminchat  # noqa: E402
import commands  # noqa: E402
import console_settings  # noqa: E402
import defaults as config  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.test_a_failed_rehash_is_not_said_to_be_in_effect import _evaluate, _if_split, handling  # noqa: E402
from tests.test_console_settings_commands import make_session, run  # noqa: E402
from tests.test_console_settings_need_the_password import PASSWORD, LockCase  # noqa: E402
from tests.test_the_mirc_settings_window import alias, dialog, statements, wrapped_lines  # noqa: E402

ADMIN_CONSOLE_DOC = os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md")

# command -> (the setting it saves, the sentence a person is told while it turns)
TOGGLES = {
    "checkupdates": ("CHECK_FOR_UPDATES", "Turning the daily update check"),
    "consolefeed": ("DEBUG_TO_CONSOLE", "Turning the console feed"),
}
UNLOCK_SENTENCE = "Type unlock <password> first - a paired token alone cannot change settings."
READ_ONLY_SENTENCE = "This console logged in with a read-only token"


def file_bytes():
    """settings.conf as the test harness redirected it, or None before any save."""
    path = settings_file.settings_path()
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


class LockedFake:
    """Enough of a structured, locked console session for console_settings."""
    structured = True
    unlocked = False

    def __init__(self):
        self.sent = []

    def send(self, line):
        self.sent.append(line)


class ToggleCase(LockCase):
    """LockCase's locked token session, with the save, the rehash and the
    background run recorded. The background run is done in place, so a save
    that happens has happened before the command returns."""

    def setUp(self):
        super().setUp()
        self.saved, self.rehashed, self.detached = [], [], []
        real_save = settings_file.save

        def save(current, changes, *args, **kwargs):
            self.saved.append(dict(changes))
            return real_save(current, changes, *args, **kwargs)

        def detached(session, label, fn):
            self.detached.append(label)
            fn()

        self.addCleanup(setattr, settings_file, "save", settings_file.save)
        settings_file.save = save
        self.addCleanup(setattr, adminchat, "_run_detached", adminchat._run_detached)
        adminchat._run_detached = detached
        # LockCase put the real one back in its own cleanup already
        commands.handle_rehash_request = lambda *a, **kw: self.rehashed.append(a)

    def assert_nothing_changed(self, key, value, before):
        self.assertEqual(self.saved, [])
        self.assertEqual(self.rehashed, [])
        self.assertEqual(self.detached, [])
        self.assertIs(getattr(config, key), value)
        self.assertEqual(file_bytes(), before)

    def assert_saved(self, command, key, wanted):
        self.assertEqual(self.detached, [command])
        self.assertEqual(self.saved, [{key: wanted}])
        self.assertEqual(len(self.rehashed), 1)
        stored = settings_file.parse(file_bytes().decode("utf-8")).get(key)
        self.assertEqual(stored, "true" if wanted else "false")


class ALockedSessionIsAskedToUnlockFirst(ToggleCase):

    def test_on_or_off_gets_one_locked_line_and_saves_nothing(self):
        for (command, (key, turning)), arg in itertools.product(TOGGLES.items(), ("on", "off", "OFF", " on ")):
            wanted = arg.strip().lower() == "on"
            self.set_config(**{key: not wanted})
            before = file_bytes()
            sent_before = len(self.session.sent)
            with self.subTest(command=command, arg=arg):
                reply = run(self.session, f"{command} {arg}")
                self.assertEqual(reply, [f"DCCORE LOCKED {command} {UNLOCK_SENTENCE}"])
                self.assertFalse([x for x in self.session.sent[sent_before:] if x.startswith(turning)])
                self.assert_nothing_changed(key, not wanted, before)

    def test_a_person_is_told_in_a_sentence(self):
        self.session.structured = False
        for command, (key, _turning) in TOGGLES.items():
            self.set_config(**{key: True})
            with self.subTest(command=command):
                self.assertEqual(run(self.session, f"{command} off"), [UNLOCK_SENTENCE])
                self.assert_nothing_changed(key, True, None)

    def test_with_no_argument_it_still_reports(self):
        for command, (key, _turning) in TOGGLES.items():
            for value, word in ((True, "on"), (False, "off")):
                self.set_config(**{key: value})
                with self.subTest(command=command, value=value):
                    self.assertEqual(run(self.session, command), [f"DCCORE {command.upper()} {word}"])
                    self.assertEqual(run(self.session, command + "   "), [f"DCCORE {command.upper()} {word}"])
        self.assert_nothing_changed("CHECK_FOR_UPDATES", False, None)

    def test_a_person_with_no_argument_is_told_the_setting(self):
        self.session.structured = False
        self.set_config(CHECK_FOR_UPDATES=True, DEBUG_TO_CONSOLE=True)
        self.assertEqual(run(self.session, "checkupdates"), ["The daily update check is on."])
        self.assertEqual(run(self.session, "consolefeed"), ["The console feed is on."])

    def test_a_bad_argument_gets_the_usage_line(self):
        for command in TOGGLES:
            with self.subTest(command=command):
                self.assertEqual(run(self.session, f"{command} sometimes"), [f"Usage: {command} [on|off]"])
        self.assert_nothing_changed("DEBUG_TO_CONSOLE", config.DEBUG_TO_CONSOLE, None)

    def test_the_session_stays_locked_for_set_too(self):
        # A control: the session these tests use is the one `set` refuses.
        self.locked(run(self.session, "set MAX_DCC_SLOTS 5"), "set")


class AnUnlockedSessionStillSaves(ToggleCase):

    def turn_off(self, session, command):
        key, turning = TOGGLES[command]
        self.set_config(**{key: True})
        reply = run(session, f"{command} off")
        self.assertEqual(reply, [f"{turning} off ...", f"DCCORE {command.upper()} off"])
        self.assert_saved(command, key, False)

    def test_a_password_login(self):
        session = make_session(self)
        for command in TOGGLES:
            self.saved, self.rehashed, self.detached = [], [], []
            with self.subTest(command=command):
                self.turn_off(session, command)

    def test_a_token_session_once_it_has_unlocked(self):
        self.assertEqual(run(self.session, "unlock " + PASSWORD), ["DCCORE UNLOCKED"])
        for command in TOGGLES:
            self.saved, self.rehashed, self.detached = [], [], []
            with self.subTest(command=command):
                self.turn_off(self.session, command)

    def test_a_wrong_password_does_not_unlock_it(self):
        run(self.session, "unlock wrong")
        self.locked(run(self.session, "checkupdates off"), "checkupdates")
        self.assertEqual(self.saved, [])

    def test_the_dashboards_console(self):
        for command, (key, turning) in TOGGLES.items():
            self.saved, self.rehashed, self.detached = [], [], []
            self.set_config(**{key: True})
            with self.subTest(command=command), contextlib.redirect_stdout(io.StringIO()):
                status, result = webserver.build_console_command_result(f"{command} off")
            self.assertEqual(status, 200)
            self.assertEqual(result["lines"][0], f"{turning} off ...")
            self.assertNotIn("DCCORE LOCKED", " ".join(result["lines"]))
            self.assert_saved(command, key, False)


class AReadOnlyTokenIsRefusedBeforeTheHandler(ToggleCase):

    def read_only_session(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        session = adminchat.Session(near, "192.0.2.1", "alfa", "alfa.example")
        session.authenticated = True
        session.structured = True
        session.read_only = True
        session.paired_as = "example-widget"
        session.sent = []
        session.send = session.sent.append
        return session

    def test_with_an_argument_the_read_only_line_and_no_handler(self):
        session = self.read_only_session()
        ran = []
        table = {name: ((lambda name: lambda s, a: ran.append(name))(name), summary, usage)
                 for name, (_fn, summary, usage) in adminchat.COMMANDS.items() if name in TOGGLES}
        with mock.patch.dict(adminchat.COMMANDS, table):
            for command in TOGGLES:
                with self.subTest(command=command):
                    reply = run(session, f"{command} off")
                    self.assertEqual(len(reply), 1, reply)
                    self.assertTrue(reply[0].startswith(f"DCCORE LOCKED {command} {READ_ONLY_SENTENCE}"), reply)
            self.assertEqual(ran, [])
            for command in TOGGLES:
                run(session, command)
            self.assertEqual(ran, list(TOGGLES))           # bare, they still report

    def test_and_nothing_is_saved(self):
        session = self.read_only_session()
        for command, (key, _turning) in TOGGLES.items():
            self.set_config(**{key: True})
            with self.subTest(command=command):
                self.assertEqual(len(run(session, f"{command} off")), 1)
                self.assert_nothing_changed(key, True, None)


class TheLockedLineIsReadAsTheOthersAre(unittest.TestCase):
    """The line the bot sends, read by the two places of dccore.mrc that read
    a LOCKED line: the settings window's dccore.sw.locked, by its first
    statement (TheAuditOfBothHalves.LOCKABLE in
    test_the_mirc_settings_window.py runs it on these too), and the
    dispatcher below."""

    def test_the_command_word_is_the_one_refused(self):
        for command in TOGGLES:
            session = LockedFake()
            self.assertTrue(console_settings.refused_while_locked(session, command))
            (line,) = session.sent
            words = line.split(" ")
            with self.subTest(command=command):
                self.assertEqual(words[:2], ["DCCORE", "LOCKED"])
                self.assertEqual(words[2], command)
                self.assertNotIn(words[3], ("commit", "resend"))      # one word, not two
                self.assertEqual(" ".join(words[3:]), UNLOCK_SENTENCE)


def bot_line(command, read_only=False):
    """$1- of dccore.structured for the LOCKED line the bot sends."""
    session = LockedFake()
    if read_only:
        console_settings.refused_read_only(session, command)
    else:
        console_settings.refused_while_locked(session, command)
    (line,) = session.sent
    assert line.startswith("DCCORE "), line
    return line[len("DCCORE "):]


def expand(statement, line):
    """`statement` with the line's fields ($2, $3- ...) put in."""
    fields = line.split(" ")

    def field(m):
        number = int(m.group(1))
        return " ".join(fields[number - 1:]) if m.group(2) else (fields[number - 1] if number <= len(fields) else "")
    return re.sub(r"\$(\d+)(-?)(?![\w.])", field, statement)


class TheScriptSaysItInDCCore(unittest.TestCase):

    def test_said_once_in_dccore_whatever_the_window_waits_for(self):
        for command, read_only, wants, tail, open_ in itertools.product(TOGGLES, (False, True), (False, True),
                                                                         (False, True), (False, True)):
            line = bot_line(command, read_only)
            path = handling("LOCKED", wants=wants, tail=tail, line=line, dialog=open_)
            with self.subTest(command=command, read_only=read_only, wants=wants, tail=tail, dialog=open_):
                self.assertEqual(len(path), 1, path)               # one statement, and it returned
                statement, ran = path[0]
                self.assertIsNotNone(ran, statement)
                self.assertTrue(ran[0].startswith("dccore.sys "), ran)
                # the window's box is put back only for the console feed, and only while the window is open
                window = ["dccore.sw.locked.toggle"] if command == "consolefeed" and open_ else []
                self.assertEqual(ran[1:], window)
                self.assertFalse(any("dccore.sw." in piece for piece in ran[:1]), ran)
                shown = expand(ran[0], line)
                sentence = UNLOCK_SENTENCE if not read_only else READ_ONLY_SENTENCE
                self.assertIn(sentence, shown)
                self.assertNotIn(command, shown.split(sentence)[0])    # the sentence, not the command word
                self.assertTrue(statement.endswith("| return } }"), statement)

    def test_other_locked_lines_go_where_they_went(self):
        for command in ("setcommit", "set", "served commit", "onconnect", "pair"):
            line = bot_line(command)
            with self.subTest(command=command, wants=True):
                path = handling("LOCKED", wants=True, tail=False, line=line)
                self.assertEqual(len(path), 1, path)
                self.assertEqual(path[0][1], ["dccore.sw.line $1-"])
            with self.subTest(command=command, tail=True):
                path = handling("LOCKED", wants=False, tail=True, line=line)
                self.assertEqual(path[0][1], ["dccore.sw.line $1-"])
            with self.subTest(command=command, wants=False):
                path = handling("LOCKED", wants=False, tail=False, line=line)
                self.assertEqual(len(path), 1, path)
                (said,) = path[0][1]
                self.assertTrue(said.startswith("dccore.sys "), said)
                self.assertIn("$iif($istok(commit resend,$3,32),$4-,$3-)", said)

    def test_the_reports_are_not_locked_lines(self):
        # A control for handling(): CHECKUPDATES and CONSOLEFEED are kept, as before.
        self.assertEqual(handling("CHECKUPDATES", False, False, "CHECKUPDATES off")[0][1],
                         ["hadd dccore.live checkupdates $2"])
        self.assertEqual(handling("CONSOLEFEED", False, False, "CONSOLEFEED on")[0][1],
                         ["hadd dccore.live consolefeed $2"])


def toggle_reset(live, box):
    """Run `alias dccore.sw.locked.toggle` on the settings window as Apply
    left it: the console feed box `box` (1 ticked, 0 not), the window's
    baseline taken with it (dccore.sw.opt.save takes it last), and the bot's
    live value `live`. What the box, the baseline and the status line are
    afterwards. A statement this does not know is an error, never skipped."""
    window = {"box": box, "sig": box, "status": []}
    atoms = {r"\$dccore\.st\(consolefeed\) == (?P<a>\w+)": lambda m: live == m.group("a")}
    taken = None                                   # the condition of the `if` just before an `else`
    for statement in statements(alias("dccore.sw.locked.toggle")):
        if statement.startswith("if "):
            condition, body = _if_split(statement)
            taken = _evaluate(condition, atoms)
            todo = [body] if taken else []
        elif statement.startswith("else {"):
            assert taken is not None, statement
            todo = [statement[statement.index("{") + 1:statement.rindex("}")].strip()] if not taken else []
            taken = None
        else:
            taken, todo = None, [statement]
        for piece in todo:
            if piece == "did -c dccore.set 1685":
                window["box"] = 1
            elif piece == "did -u dccore.set 1685":
                window["box"] = 0
            elif piece == "hadd dccore.sws opt.sig $dccore.sw.opt.sig":
                window["sig"] = window["box"]
            elif piece.startswith("dccore.sw.status "):
                window["status"].append(piece[len("dccore.sw.status "):])
            else:
                raise AssertionError("not understood: %r" % piece)
    return window


class TheWindowsConsoleFeedBoxIsPutBack(unittest.TestCase):
    """The settings window's console feed box (1685) sends `consolefeed
    on|off` on Apply. Refused on a locked console, the box is put back as the
    bot has it, the window's baseline taken again with it so the box is not
    an unsaved change, and the window says why."""

    def test_the_box_shows_the_bots_value_and_is_not_left_as_a_change(self):
        for live in ("on", "off"):
            refused = 0 if live == "on" else 1     # what Apply sent, the other one
            window = toggle_reset(live, refused)
            with self.subTest(live=live):
                self.assertEqual(window["box"], 1 if live == "on" else 0)
                self.assertEqual(window["sig"], window["box"])         # dccore.sw.opt.dirty: no change left

    def test_the_reset_reads_the_box_and_the_value_as_the_window_does(self):
        # The control the reset writes is the one the baseline is taken of,
        # and the value is the one dccore.sw.opt.fill ticks it from.
        self.assertIn("$did(dccore.set,1685).state", statements(alias("dccore.sw.opt.sig"))[-1])
        self.assertIn("if ($dccore.st(consolefeed) == on) { did -c dccore.set 1685 }",
                      statements(alias("dccore.sw.opt.fill")))
        self.assertIn("hadd dccore.sws opt.sig $dccore.sw.opt.sig", statements(alias("dccore.sw.opt.save")))
        self.assertIn("$dccore.sw.s(opt.sig)", alias("dccore.sw.opt.dirty"))

    def test_the_window_says_to_unlock_in_dccore_on_its_one_line(self):
        for live in ("on", "off"):
            (status,) = toggle_reset(live, 1 if live == "off" else 0)["status"]
            with self.subTest(live=live):
                self.assertIn("unlock <password>", status)
                self.assertIn("@DCCore", status)
                self.assertIn("Apply", status)
                self.assertNotRegex(status, r"[$%|]", status)          # literal text only
                control = [c for c in dialog("dccore.set") if c[2] == 1018]
                self.assertEqual(len(control), 1)
                self.assertEqual(wrapped_lines(status, control[0][5]), 1, status)


class TheProtocolIsDocumented(unittest.TestCase):

    def test_the_locked_row_lists_both(self):
        with io.open(ADMIN_CONSOLE_DOC, encoding="utf-8") as handle:
            rows = [x for x in handle.read().split("\n") if x.startswith("| `DCCORE LOCKED <command> <sentence>`")]
        self.assertEqual(len(rows), 1, rows)
        for command in TOGGLES:
            word = bot_line(command).split(" ")[1]
            self.assertIn(f"`{word}`", rows[0])


if __name__ == "__main__":
    unittest.main()
