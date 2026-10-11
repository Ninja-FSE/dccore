"""The bot side of dccore.mrc's settings window (#1264, phase A).

The window does its work over the admin console, through the dashboard's
OWN functions: `settings` / `set` / `setbegin` / `setcommit` over
build_settings_payload() and apply_settings_changes(), `served` over the
lists endpoints, `folders` over the folder ones, `onconnect` over the
on-connect trio, `setpreview` over the theme preview, and `banlist` for the
bans. These tests hold the line protocol to what docs/ADMIN-CONSOLE.md
promises - framing and counts, the value encoding, that a value sent back
unchanged changes nothing, that a commit is ONE save - and keep the commands
where they belong: the console, never a channel.
"""

import contextlib
import io
import os
import socket
import sys
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
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, parse_source  # noqa: E402

# The real save, kept before any test stands a recorder in its place.
_real_apply = webserver.apply_settings_changes


def make_session(case, structured=True):
    near, far = socket.socketpair()
    case.addCleanup(near.close)
    case.addCleanup(far.close)
    session = adminchat.Session(near, "192.0.2.1", "alfa", "alfa.example")
    session.authenticated = True
    session.unlocked = True          # logged in with the password
    session.structured = structured
    session.sent = []
    session.send = session.sent.append
    return session


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def run(session, line):
    """One console line, as the reader loop hands it over; returns the
    replies it produced and keeps the bot's log out of the test output."""
    before = len(session.sent)
    with contextlib.redirect_stdout(io.StringIO()):
        adminchat.handle_command(session, line)
    return session.sent[before:]


def logged(session, line):
    """What handle_command() printed to the bot's log for `line`."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        adminchat.handle_command(session, line)
    return out.getvalue()


def rows(lines, kind):
    prefix = f"DCCORE {kind} "
    return [line[len(prefix):] for line in lines if line.startswith(prefix)]


def setf(lines):
    """{KEY: (type, decoded value)} from a `settings` snapshot."""
    found = {}
    for row in rows(lines, "SETF"):
        parts = row.split(" ", 2)
        found[parts[0]] = (parts[1], console_settings.decode_value(parts[2] if len(parts) > 2 else ""))
    return found


def as_input(lines):
    """A `served` / `folders` / `onconnect` snapshot turned back into the
    console lines that rebuild it - the window's whole job, mechanically."""
    out = []
    for line in lines:
        for kind, command in (("SRVLIST", "served list"), ("SRVCHAN", "served chan"),
                              ("SRVFOLDER", "served folder"), ("FLDROW", "folders row"),
                              ("OCLINE", "onconnect line")):
            if line.startswith(f"DCCORE {kind} "):
                out.append(command + " " + line[len(f"DCCORE {kind} "):])
    return out


class Recorder:
    """Stands in for webserver.apply_settings_changes(): records each call
    and answers as the real one does on success."""

    def __init__(self):
        self.calls = []
        self.sources = []

    def __call__(self, changes, source=None, on_applied=None):
        self.calls.append(dict(changes))
        self.sources.append(source)
        return 200, {"written": sorted(k for k in changes if k != "confirm_debug_channel_removed"),
                     "rehash": "started", "restart_required": []}


class SettingsCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.session = make_session(self)
        self.apply = Recorder()
        real = webserver.apply_settings_changes
        webserver.apply_settings_changes = self.apply
        self.addCleanup(setattr, webserver, "apply_settings_changes", real)
        real_rehash = commands.handle_rehash_request
        commands.handle_rehash_request = lambda *a, **kw: None
        self.addCleanup(setattr, commands, "handle_rehash_request", real_rehash)

    def snapshot(self):
        return run(self.session, "settings")

    def page_fields(self):
        return [f for c in webserver.build_settings_payload()["categories"] for f in c["fields"]]


# --------------------------------------------------------------------------
# The value encoding
# --------------------------------------------------------------------------

AWKWARD = ["", " ", "-", "plain", "Some Album", "a  b", "  lead", "trail  ", "50% off", "%41",
           "%%41", "%2541", "%", "%4", "100%", "%admin%", "%20", "%%20", "%2D", "%7f", "%1a", "\\x0304,01", "\x0304,01 colour", "tab\there",
           "nl\nhere", "caf\u00e9 \u00fc", "C:\\Music  Folder\\x", "#music,#rock", "a %20 b"]


class TheValueEncoding(unittest.TestCase):

    def test_every_value_comes_back_exactly(self):
        for value in AWKWARD:
            with self.subTest(value=value):
                self.assertEqual(console_settings.decode_value(console_settings.encode_value(value)), value)
                self.assertEqual(console_settings.decode_token(console_settings.encode_token(value)), value)

    def test_an_ordinary_value_crosses_unchanged(self):
        for value in ("plain", "Some Album", "50% off", "100%", "%admin%", "%nick%", "%41", "\\x0304,01", "#music,#rock", "C:\\Music\\x"):
            with self.subTest(value=value):
                self.assertEqual(console_settings.encode_value(value), value)

    def test_nothing_on_the_line_depends_on_spaces_or_control_codes_surviving(self):
        for value in AWKWARD:
            encoded = console_settings.encode_value(value)
            with self.subTest(value=value):
                self.assertEqual(encoded, encoded.strip())
                self.assertNotIn("  ", encoded)
                self.assertFalse(any(ord(ch) < 32 or ord(ch) == 127 for ch in encoded))

    def test_a_token_is_one_word_and_empty_is_a_dash(self):
        self.assertEqual(console_settings.encode_token(""), "-")
        self.assertEqual(console_settings.encode_token("-"), "%2D")
        for value in AWKWARD:
            with self.subTest(value=value):
                token = console_settings.encode_token(value)
                self.assertTrue(token)
                self.assertNotIn(" ", token)


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

class TheSettingsSnapshot(SettingsCase):

    def test_it_is_framed_with_the_count_at_both_ends(self):
        lines = self.snapshot()
        count = len(self.page_fields())
        self.assertEqual(lines[0], f"DCCORE SETBEGIN {count}")
        self.assertEqual(lines[-1], f"DCCORE SETEND {count}")
        self.assertEqual(len(rows(lines, "SETF")), count)
        self.assertEqual(len(lines), count + 2)

    def test_it_is_exactly_the_fields_the_settings_page_gets(self):
        found = setf(self.snapshot())
        for field in self.page_fields():
            with self.subTest(name=field["name"]):
                self.assertIn(field["name"], found)
                self.assertEqual(found[field["name"]][0], field["type"])
        self.assertEqual(set(found), {f["name"] for f in self.page_fields()})

    def test_a_value_is_its_settings_conf_form(self):
        self.set_config(CHANNEL="#music, #rock", SEARCH_ENABLED=False, CUSTOM_THEME_BORDER="\x0304,01")
        found = setf(self.snapshot())
        self.assertEqual(found["CHANNEL"], ("str", "#music, #rock"))
        self.assertEqual(found["SEARCH_ENABLED"], ("bool", "false"))
        self.assertEqual(found["CUSTOM_THEME_BORDER"][1], "\\x0304,01")

    def test_an_empty_value_is_a_line_with_nothing_after_the_type(self):
        self.set_config(DEBUG_CHANNEL="")
        (line,) = [l for l in self.snapshot() if l.startswith("DCCORE SETF DEBUG_CHANNEL ")]
        self.assertEqual(line, "DCCORE SETF DEBUG_CHANNEL str")

    def test_the_password_hash_never_appears(self):
        secret = adminchat.make_password_hash("hunter2", iterations=1000)
        self.set_config(ADMIN_PASSWORD_HASH=secret)
        for structured in (True, False):
            session = make_session(self, structured=structured)
            text = "\n".join(run(session, "settings"))
            with self.subTest(structured=structured):
                self.assertNotIn("ADMIN_PASSWORD_HASH", text)
                self.assertNotIn(secret, text)

    def test_it_stays_well_inside_the_outbox(self):
        self.assertLess(len(self.snapshot()), adminchat.OUTBOX_MAX // 2)

    def test_it_reads_the_payload_under_the_reload_lock(self):
        import runtime
        import threading
        seen = []
        real = webserver._settings_payload_unlocked

        def held_by_someone():
            # RLock.locked() is new in Python 3.14, and an RLock is free to
            # the thread that owns it. Another thread that cannot take it
            # proves it is held; one that can gives it straight back.
            took = []

            def probe():
                got = runtime.config_reload_lock.acquire(blocking=False)
                took.append(got)
                if got:
                    runtime.config_reload_lock.release()
            prober = threading.Thread(target=probe)
            prober.start()
            prober.join()
            return not took[0]

        def spy(settings_module):
            seen.append(held_by_someone())
            return real(settings_module)
        webserver._settings_payload_unlocked = spy
        self.addCleanup(setattr, webserver, "_settings_payload_unlocked", real)
        self.snapshot()
        self.assertEqual(seen, [True])

    def test_a_person_gets_key_equals_value_and_can_filter(self):
        self.set_config(NICKNAME="alfa")
        session = make_session(self, structured=False)
        out = run(session, "settings nickname")
        self.assertIn("  NICKNAME = alfa", out)
        self.assertFalse(any(line.startswith("DCCORE") for line in out))
        self.assertTrue(all("NICKNAME" in line or not line.startswith("  ") for line in out))


class EverySettingRoundTrips(SettingsCase):
    """The whole snapshot, sent back as the window would send it, changes
    nothing: no save, no rehash, not a byte of settings.conf."""

    def test_every_value_sent_back_is_recognised_as_unchanged(self):
        path = settings_file.settings_path()
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("NICKNAME = alfa\n")
        self.set_config(NICKNAME="alfa")            # what the daemon read from that file
        before_file = read_bytes(path)
        found = setf(self.snapshot())
        before = {name: getattr(config, name) for name in found}

        run(self.session, "setbegin")
        errors = []
        for name, (_type, value) in found.items():
            errors += rows(run(self.session, f"set {name} {console_settings.encode_value(value)}"), "SETERR")
        self.assertEqual(errors, [])
        reply = run(self.session, "setcommit")

        self.assertEqual(reply, [f"DCCORE SETDONE ok 0 {len(found)} - Nothing changed; nothing was saved."])
        self.assertEqual(self.apply.calls, [])
        self.assertEqual({name: getattr(config, name) for name in found}, before)
        self.assertEqual(read_bytes(path), before_file)

    def test_each_value_written_through_the_real_save_reads_back_the_same(self):
        """And not only "recognised": every decoded value, put through the
        save the Settings page uses, is a value settings.conf carries back
        unchanged - so the form IS the settings.conf form."""
        found = setf(self.snapshot())
        for name, (_type, value) in found.items():
            with self.subTest(name=name):
                self.assertEqual(settings_file.check_change(vars(config), name, value), getattr(config, name))


class ATransaction(SettingsCase):

    def test_a_commit_is_one_save_of_everything_that_changed(self):
        self.set_config(MAX_DCC_SLOTS=2, NICKNAME="alfa")
        run(self.session, "setbegin")
        self.assertEqual(run(self.session, "set MAX_DCC_SLOTS 5"), [])
        self.assertEqual(run(self.session, "set NICKNAME alfa"), [])
        self.assertEqual(run(self.session, "set SEARCH_ENABLED false"), [])
        self.assertEqual(self.apply.calls, [])
        reply = run(self.session, "setcommit")
        self.assertEqual(self.apply.calls, [{"MAX_DCC_SLOTS": "5", "SEARCH_ENABLED": "false"}])
        self.assertEqual(len(reply), 1)
        self.assertTrue(reply[0].startswith("DCCORE SETDONE ok 2 1 - Saved 2 setting(s)"), reply)
        self.assertIsNone(self.session.settings_txn)

    def test_it_really_writes_settings_conf_once_through_the_dashboards_save(self):
        webserver.apply_settings_changes = _real_apply
        saves = []
        real_save = settings_file.save

        def counting_save(namespace, changes, path=None, log=print):
            saves.append(sorted(changes))
            return real_save(namespace, changes, path=path, log=lambda *_a: None)
        settings_file.save = counting_save
        self.addCleanup(setattr, settings_file, "save", real_save)
        self.set_config(MAX_DCC_SLOTS=2, MAX_USER_QUEUE=10)

        run(self.session, "setbegin")
        run(self.session, "set MAX_DCC_SLOTS 4")
        run(self.session, "set MAX_USER_QUEUE 12")
        reply = run(self.session, "setcommit")

        self.assertTrue(reply[0].startswith("DCCORE SETDONE ok 2 0 "), reply)
        self.assertEqual(saves, [["MAX_DCC_SLOTS", "MAX_USER_QUEUE"]])
        written = settings_file.parse(read_bytes(settings_file.settings_path()).decode("utf-8"))
        self.assertEqual((written["MAX_DCC_SLOTS"], written["MAX_USER_QUEUE"]), ("4", "12"))

    def test_a_bad_value_is_refused_by_name_and_nothing_is_saved(self):
        run(self.session, "setbegin")
        run(self.session, "set MAX_DCC_SLOTS 5")
        bad = run(self.session, "set MAX_USER_QUEUE lots")
        self.assertEqual(len(bad), 1)
        self.assertTrue(bad[0].startswith("DCCORE SETERR MAX_USER_QUEUE "), bad)
        self.assertIn("whole number", bad[0])
        reply = run(self.session, "setcommit")
        self.assertEqual(reply, ["DCCORE SETDONE error 1 setting(s) refused (MAX_USER_QUEUE); nothing was saved."])
        self.assertEqual(self.apply.calls, [])
        self.assertIsNone(self.session.settings_txn)

    def test_each_refusal_is_in_the_dashboards_words(self):
        run(self.session, "setbegin")
        reply = run(self.session, "set NICKNAME Music Bot")
        try:
            settings_file.check_change(vars(config), "NICKNAME", "Music Bot")
        except settings_file.SettingsWriteError as err:
            expected = str(err)
        self.assertEqual(reply, [f"DCCORE SETERR NICKNAME {expected}"])

    def test_an_unknown_key_is_refused(self):
        run(self.session, "setbegin")
        reply = run(self.session, "set NOT_A_SETTING 1")
        self.assertEqual(len(reply), 1)
        self.assertTrue(reply[0].startswith("DCCORE SETERR NOT_A_SETTING "))

    def test_the_password_hash_is_refused_and_its_value_never_echoed_or_logged(self):
        run(self.session, "setbegin")
        log = logged(self.session, "set ADMIN_PASSWORD_HASH pbkdf2$sekrit")
        reply = self.session.sent[-1]
        self.assertTrue(reply.startswith("DCCORE SETERR ADMIN_PASSWORD_HASH "), reply)
        self.assertIn("password form", reply)
        self.assertNotIn("sekrit", reply)
        self.assertNotIn("sekrit", log)
        run(self.session, "setcommit")
        self.assertEqual(self.apply.calls, [])

    def test_a_set_line_is_never_logged_with_its_value(self):
        self.assertEqual(logged(self.session, "set MAX_DCC_SLOTS 3"), "")

    def test_abort_drops_everything(self):
        run(self.session, "setbegin")
        run(self.session, "set MAX_DCC_SLOTS 7")
        self.assertEqual(run(self.session, "setabort"), ["DCCORE SETDONE aborted 1"])
        self.assertEqual(run(self.session, "setcommit"),
                         ["DCCORE SETDONE error No transaction is open - send setbegin first."])
        self.assertEqual(self.apply.calls, [])

    def test_a_second_setbegin_replaces_the_first_and_says_how_much_it_dropped(self):
        run(self.session, "setbegin")
        run(self.session, "set MAX_DCC_SLOTS 7")
        self.assertEqual(run(self.session, "setbegin"), ["DCCORE SETOPEN 1"])
        run(self.session, "setcommit")
        self.assertEqual(self.apply.calls, [])

    def test_a_transaction_dies_with_its_session(self):
        run(self.session, "setbegin")
        run(self.session, "set MAX_DCC_SLOTS 7")
        fresh = make_session(self)
        self.assertIsNone(fresh.settings_txn)
        self.assertEqual(run(fresh, "setcommit"),
                         ["DCCORE SETDONE error No transaction is open - send setbegin first."])

    def test_outside_a_transaction_set_saves_at_once(self):
        self.set_config(MAX_DCC_SLOTS=2)
        reply = run(self.session, "set MAX_DCC_SLOTS 3")
        self.assertEqual(self.apply.calls, [{"MAX_DCC_SLOTS": "3"}])
        self.assertTrue(reply[-1].startswith("DCCORE SETDONE ok 1 0 - "), reply)

    def test_restart_only_settings_are_named(self):
        webserver.apply_settings_changes = lambda changes, source=None, on_applied=None: (
            200, {"written": sorted(changes), "restart_required": ["WEBUI_PORT"]})
        reply = run(self.session, "set WEBUI_PORT 8421")
        self.assertTrue(reply[-1].startswith("DCCORE SETDONE ok 1 0 WEBUI_PORT Saved 1 setting(s)"), reply)

    def test_a_person_is_answered_in_sentences(self):
        session = make_session(self, structured=False)
        self.set_config(MAX_DCC_SLOTS=2)
        reply = run(session, "set MAX_DCC_SLOTS 3")
        self.assertEqual(reply, ["Saved 1 setting(s); the bot is rehashing to apply them."])


class ClearingTheDebugChannelAsksFirst(SettingsCase):

    def setUp(self):
        super().setUp()
        self.set_config(DEBUG_CHANNEL="#music-debug")

    def test_the_commit_asks_and_keeps_the_transaction(self):
        run(self.session, "setbegin")
        run(self.session, "set DEBUG_CHANNEL")
        reply = run(self.session, "setcommit")
        self.assertEqual(len(reply), 1)
        self.assertTrue(reply[0].startswith("DCCORE SETDONE confirm Remove the debug channel, #music-debug?"), reply)
        self.assertEqual(self.apply.calls, [])
        self.assertIsNotNone(self.session.settings_txn)

        reply = run(self.session, "setcommit confirm")
        self.assertEqual(self.apply.calls, [{"DEBUG_CHANNEL": "", "confirm_debug_channel_removed": True}])
        self.assertTrue(reply[0].startswith("DCCORE SETDONE ok 1 0 "), reply)

    def test_a_single_set_asks_too_and_setcommit_confirm_answers_it(self):
        reply = run(self.session, "set DEBUG_CHANNEL")
        self.assertTrue(reply[0].startswith("DCCORE SETDONE confirm "), reply)
        self.assertEqual(self.apply.calls, [])
        run(self.session, "setcommit confirm")
        self.assertEqual(len(self.apply.calls), 1)

    def test_moving_it_to_another_channel_does_not_ask(self):
        run(self.session, "set DEBUG_CHANNEL #music-ops")
        self.assertEqual(self.apply.calls, [{"DEBUG_CHANNEL": "#music-ops"}])


class ThePreviewUsesTheUnsavedValues(SettingsCase):

    def preview(self):
        lines = run(self.session, "setpreview")
        self.assertEqual(lines[0], "DCCORE PVBEGIN 2")
        self.assertEqual(lines[-1], "DCCORE PVEND 2")
        return {kind: console_settings.decode_value(text)
                for kind, text in (row.split(" ", 1) for row in rows(lines, "PVLINE"))}

    def test_a_run_of_spaces_survives_the_trip(self):
        # mIRC collapses runs of spaces in a chat line's $1-, and a theme's
        # frame is runs of coloured spaces: the line must carry them encoded.
        lines = run(self.session, "setpreview")
        for row in rows(lines, "PVLINE"):
            with self.subTest(row=row[:20]):
                self.assertNotIn("  ", row)
                self.assertNotRegex(row, "[\x00-\x1f]")

    def test_with_no_transaction_it_is_the_saved_theme(self):
        expected = webserver.build_theme_preview(webserver.theme_preview_overrides({}))
        self.assertEqual(self.preview(), {"advert": expected["advert"], "notice": expected["notice"]})

    def test_a_buffered_theme_and_colour_are_what_it_draws(self):
        self.set_config(THEME="classic", CUSTOM_THEME_BORDER="")
        saved = self.preview()
        run(self.session, "setbegin")
        run(self.session, "set THEME midnight")
        run(self.session, "set CUSTOM_THEME_ACCENT \\x0304")
        shown = self.preview()
        expected = webserver.build_theme_preview(webserver.theme_preview_overrides(
            {"THEME": "midnight", "CUSTOM_THEME_ACCENT": "\\x0304"}))
        self.assertEqual(shown, {"advert": expected["advert"], "notice": expected["notice"]})
        self.assertNotEqual(shown, saved)
        self.assertIn("\x0304", shown["notice"])
        self.assertEqual(config.THEME, "classic")
        self.assertEqual(self.apply.calls, [])

    def test_a_buffered_search_switch_is_a_real_boolean(self):
        self.set_config(SEARCH_ENABLED=True)
        run(self.session, "setbegin")
        run(self.session, "set SEARCH_ENABLED false")
        expected = webserver.build_theme_preview(webserver.theme_preview_overrides({"SEARCH_ENABLED": False}))
        self.assertEqual(self.preview()["advert"], expected["advert"])
        self.assertIn("OFF", adminchat.strip_irc_formatting(self.preview()["advert"]))


# --------------------------------------------------------------------------
# served lists, folders, on-connect
# --------------------------------------------------------------------------

class ServedCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.session = make_session(self)
        self.root = self.make_temp_dir()
        self.music = os.path.join(self.root, "Music  Folder")
        self.rock = os.path.join(self.root, "rock")
        for path in (self.music, self.rock):
            os.makedirs(path)


class TheServedLists(ServedCase):

    def write_lists(self):
        library.save_lists([
            library.ServedList("Main", True, ["#music", "#music-chat"],
                               [library.Folder("All the music", self.music)], {"#music-chat": "quiet"}),
            library.ServedList("Rock  Only", False, ["#rock"],
                               [library.Folder("-", self.rock)], {"#rock": "request_only"}),
        ])

    def test_the_snapshot_is_framed_with_its_totals(self):
        self.write_lists()
        lines = run(self.session, "served")
        self.assertEqual(lines[0], f"DCCORE SRVBEGIN 2 file {webserver.MAX_SERVED_LISTS}")
        self.assertEqual(lines[-1], "DCCORE SRVEND 2 3 2")
        self.assertEqual(rows(lines, "SRVLIST"), ["1 1 Main", "2 0 Rock %20Only"])
        self.assertEqual(rows(lines, "SRVCHAN"), ["1 #music normal", "1 #music-chat quiet", "2 #rock request_only"])
        self.assertEqual(rows(lines, "SRVFOLDER")[1], f"2 %2D {console_settings.encode_value(self.rock)}")

    def test_sent_back_unchanged_it_is_the_same_payload_and_nothing_is_written(self):
        self.write_lists()
        before = read_bytes(library.lists_file())
        payload = webserver.build_lists_payload()
        lines = run(self.session, "served")
        run(self.session, "served begin")
        for line in as_input(lines):
            self.assertEqual(run(self.session, line), [], line)
        self.assertEqual(self.session.served_txn.lists, payload["lists"])
        self.assertEqual(run(self.session, "served commit"),
                         ["DCCORE SRVDONE unchanged Nothing changed; nothing was saved."])
        self.assertEqual(read_bytes(library.lists_file()), before)

    def test_and_saved_anyway_through_the_dashboards_save_it_reads_back_identical(self):
        self.write_lists()
        payload = webserver.build_lists_payload()
        lines = run(self.session, "served")
        run(self.session, "served begin")
        for line in as_input(lines):
            run(self.session, line)
        status, _result = webserver.apply_list_changes({"lists": self.session.served_txn.lists})
        self.assertEqual(status, 200)
        self.assertEqual(webserver.build_lists_payload(), payload)

    def test_the_implied_list_sent_back_does_not_create_a_lists_file(self):
        library.save_folders([library.Folder("Music", self.music)])
        lines = run(self.session, "served")
        self.assertTrue(lines[0].startswith("DCCORE SRVBEGIN 1 implied "), lines[0])
        run(self.session, "served begin")
        for line in as_input(lines):
            run(self.session, line)
        self.assertEqual(run(self.session, "served commit")[0].split()[2], "unchanged")
        self.assertFalse(os.path.exists(library.lists_file()))

    def test_a_change_is_saved_with_the_dashboards_message(self):
        self.write_lists()
        run(self.session, "served begin")
        run(self.session, "served list 1 1 Main")
        run(self.session, f"served folder 1 Music {console_settings.encode_value(self.music)}")
        run(self.session, "served chan 1 #music quiet")
        reply = run(self.session, "served commit")
        self.assertEqual(reply, ["DCCORE SRVDONE ok 1 Saved. Rebuild the lists to publish them."])
        (row,) = webserver.build_lists_payload()["lists"]
        self.assertEqual((row["channels"], row["modes"]), (["#music"], {"#music": "quiet"}))

    def test_the_dashboards_refusals_come_back_one_per_line(self):
        run(self.session, "served begin")
        run(self.session, "served list 1 0 Main")
        run(self.session, "served chan 1 music normal")
        reply = run(self.session, "served commit")
        self.assertEqual(len(rows(reply, "SRVERR")), 2, reply)
        self.assertEqual(reply[-1], "DCCORE SRVDONE error 2 problems; nothing was saved.")
        self.assertFalse(os.path.exists(library.lists_file()))

    def test_a_malformed_row_spoils_the_commit(self):
        run(self.session, "served begin")
        self.assertTrue(run(self.session, "served list 2 1 Main")[0].startswith("DCCORE SRVERR "))
        self.assertTrue(run(self.session, "served chan 1 #music normal")[0].startswith("DCCORE SRVERR "))
        run(self.session, "served list 1 1 Main")
        self.assertTrue(run(self.session, "served chan 1 #music loud")[0].startswith("DCCORE SRVERR "))
        self.assertEqual(run(self.session, "served commit"),
                         ["DCCORE SRVDONE error 3 line(s) refused; nothing was saved."])
        self.assertFalse(os.path.exists(library.lists_file()))

    def test_abort_and_no_transaction(self):
        self.assertEqual(run(self.session, "served commit"),
                         ["DCCORE SRVDONE error No lists transaction is open - send served begin first."])
        run(self.session, "served begin")
        self.assertEqual(run(self.session, "served abort"), ["DCCORE SRVDONE aborted 0"])

    def test_the_rows_are_not_logged(self):
        run(self.session, "served begin")
        self.assertEqual(logged(self.session, "served list 1 1 Main"), "")
        self.assertIn("ran: served begin", logged(self.session, "served begin"))


class TheServedFolders(ServedCase):

    def test_sent_back_unchanged_nothing_is_written(self):
        library.save_folders([library.Folder("All  of it", self.music), library.Folder("-", self.rock)])
        before = read_bytes(library.folders_file())
        lines = run(self.session, "folders")
        self.assertEqual(lines[0], "DCCORE FLDBEGIN 2 file")
        self.assertEqual(lines[-1], "DCCORE FLDEND 2")
        run(self.session, "folders begin")
        for line in as_input(lines):
            self.assertEqual(run(self.session, line), [], line)
        self.assertEqual(self.session.folders_txn.folders, webserver.build_folders_payload()["folders"])
        self.assertEqual(run(self.session, "folders commit"),
                         ["DCCORE FLDDONE unchanged Nothing changed; nothing was saved."])
        self.assertEqual(read_bytes(library.folders_file()), before)

    def test_a_change_goes_through_the_dashboards_save(self):
        run(self.session, "folders begin")
        run(self.session, f"folders row 1 Rock {console_settings.encode_value(self.rock)}")
        reply = run(self.session, "folders commit")
        self.assertEqual(reply[0].split()[:4], ["DCCORE", "FLDDONE", "ok", "1"])
        self.assertEqual(webserver.build_folders_payload()["folders"], [{"name": "Rock", "path": self.rock}])

    def test_its_refusal_names_each_problem(self):
        run(self.session, "folders begin")
        run(self.session, f"folders row 1 Same {console_settings.encode_value(self.music)}")
        run(self.session, f"folders row 2 Same {console_settings.encode_value(self.rock)}")
        reply = run(self.session, "folders commit")
        self.assertTrue(rows(reply, "FLDERR"), reply)
        self.assertTrue(reply[-1].startswith("DCCORE FLDDONE error "), reply)


class TheOnConnectCommands(DCCoreTestCase):

    LOGIN = "PRIVMSG X@channels.example :LOGIN alfa hunter2  %20 %nick%"

    def setUp(self):
        super().setUp()
        self.session = make_session(self)
        on_connect.save([self.LOGIN, "MODE %nick% +x"], 3)

    def test_the_snapshot_and_its_round_trip(self):
        before = read_bytes(on_connect.on_connect_file())
        lines = run(self.session, "onconnect")
        self.assertEqual(lines[0], f"DCCORE OCBEGIN 2 3 {on_connect.MAX_COMMANDS} {on_connect.MAX_DELAY_SECONDS}")
        self.assertEqual(lines[-1], "DCCORE OCEND 2")
        self.assertEqual(rows(lines, "OCLINE")[0], "1 PRIVMSG X@channels.example :LOGIN alfa hunter2 %20%2520 %nick%")
        run(self.session, "onconnect begin")
        for line in as_input(lines):
            self.assertEqual(run(self.session, line), [], line)
        self.assertEqual(self.session.onconnect_txn.commands, [self.LOGIN, "MODE %nick% +x"])
        self.assertEqual(run(self.session, "onconnect commit"),
                         ["DCCORE OCDONE unchanged Nothing changed; nothing was saved."])
        self.assertEqual(read_bytes(on_connect.on_connect_file()), before)

    def test_a_change_and_the_delay_go_through_the_dashboards_save(self):
        run(self.session, "onconnect begin")
        run(self.session, "onconnect delay 5")
        run(self.session, "onconnect line 1 MODE %nick% +x")
        reply = run(self.session, "onconnect commit")
        self.assertTrue(reply[0].startswith("DCCORE OCDONE ok 1 "), reply)
        self.assertEqual(on_connect.load(), (["MODE %nick% +x"], 5.0))

    def test_no_line_ever_reaches_the_log(self):
        run(self.session, "onconnect begin")
        out = logged(self.session, f"onconnect line 1 {self.LOGIN}")
        out += logged(self.session, "onconnect commit")
        self.assertNotIn("hunter2", out)

    def test_a_refusal_never_echoes_the_command(self):
        run(self.session, "onconnect begin")
        reply = run(self.session, "onconnect line 2 PRIVMSG X :LOGIN alfa hunter2")
        self.assertTrue(reply[0].startswith("DCCORE OCERR "))
        self.assertNotIn("hunter2", reply[0])

    def test_resend_is_the_dashboards_answer(self):
        reply = run(self.session, "onconnect resend")
        status, result = webserver.build_on_connect_resend_result()
        self.assertNotEqual(status, 200)
        self.assertEqual(reply, [f"DCCORE OCRESEND error {result['error']}"])


# --------------------------------------------------------------------------
# banlist, consolecaps
# --------------------------------------------------------------------------

class TheBanList(DCCoreTestCase):

    def test_both_kinds_framed_with_their_counts(self):
        import db
        db.add_hard_ban("*!*@spam.example")
        config.banned_users["alfa"] = time.time() + 600
        config.banned_users["bravo"] = time.time() - 5
        session = make_session(self)
        lines = run(session, "banlist")
        self.assertEqual(lines[0], "DCCORE BANBEGIN 1 1")
        self.assertEqual(rows(lines, "BANP"), ["*!*@spam.example"])
        (timed,) = rows(lines, "BANT")
        seconds, nick = timed.split()
        self.assertEqual(nick, "alfa")
        self.assertTrue(590 <= int(seconds) <= 600, seconds)
        self.assertEqual(lines[-1], "DCCORE BANEND 1 1")

    def test_a_huge_ban_file_is_capped_but_counted(self):
        patterns = [f"*!*@host{n}.example" for n in range(console_settings.BANLIST_MAX + 3)]
        lines = console_settings.banlist_lines(patterns, [])
        self.assertEqual(len(rows(lines, "BANP")), console_settings.BANLIST_MAX)
        self.assertEqual(lines[-1], f"DCCORE BANEND {console_settings.BANLIST_MAX + 3} 0")


class TheCapabilities(DCCoreTestCase):

    def test_consolecaps_names_every_part_with_its_version(self):
        reply = run(make_session(self), "consolecaps")
        self.assertEqual(reply, ["DCCORE CAPS settings:1 preview:1 served:1 folders:1 onconnect:1 banlist:1 unlock:1"
                                 " machine:" + console_settings.machine_name()])

    def test_the_machine_name_is_one_token_naming_this_computer(self):
        import socket
        name = console_settings.machine_name()
        self.assertEqual(name.lower(), socket.gethostname().lower().replace(" ", "-").replace(":", "-") or "-")
        self.assertNotIn(" ", name)
        self.assertNotIn(":", name)

    def test_an_awkward_machine_name_still_makes_one_token(self):
        import socket
        real = socket.gethostname
        socket.gethostname = lambda: "Living Room:PC\x01"
        self.addCleanup(setattr, socket, "gethostname", real)
        self.assertEqual(console_settings.machine_name(), "Living-Room-PC-")


# --------------------------------------------------------------------------
# Where these commands can be reached from
# --------------------------------------------------------------------------

NEW_COMMANDS = ("settings", "set", "setbegin", "setcommit", "setabort", "setpreview",
                "served", "folders", "onconnect", "banlist", "consolecaps")


def source(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def reaches(text, filename):
    """What in this source can run a console command: a call of
    handle_command(), a read of adminchat.COMMANDS or of a console handler,
    an import of console_settings. Read from the code, not the text - a
    docstring that names one is not a caller."""
    import ast
    found = set()
    for node in ast.walk(parse_source(text, filename=filename)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "handle_command":
                found.add("handle_command")
        elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
              and node.value.id == "adminchat"
              and (node.attr == "COMMANDS" or node.attr.startswith("_cmd_"))):
            found.add("adminchat." + node.attr)
        elif isinstance(node, ast.Import) and any(a.name == "console_settings" for a in node.names):
            found.add("console_settings")
        elif isinstance(node, ast.ImportFrom) and node.module == "console_settings":
            found.add("console_settings")
    return found


class OnlyTheConsoleReachesThem(DCCoreTestCase):

    def test_only_the_console_and_the_dashboards_console_dispatch_console_commands(self):
        """The in-channel admin commands are a fixed handful in irc.py; none
        of the paths a channel or a private message takes ever reaches
        adminchat's command table, or this module."""
        src = os.path.join(REPO_ROOT, "src")
        reaching = {}
        for name in sorted(os.listdir(src)) + ["../oserve.py"]:
            if name.endswith(".py"):
                reaching[name] = reaches(source("src", name), name)
        self.assertEqual({name: found for name, found in reaching.items() if found},
                         {"adminchat.py": {"handle_command", "console_settings"},
                          "webserver.py": {"handle_command"}})

    def test_the_channel_gate_dispatches_none_of_them(self):
        gate = source("src", "irc.py").split("ADMIN_CHANNEL_COMMANDS", 1)[1]
        gate = gate[:gate.find("elif any(msg_lower.startswith")]
        for word in NEW_COMMANDS + ("apply_settings_changes", "apply_list_changes", "apply_on_connect_changes"):
            with self.subTest(word=word):
                self.assertNotRegex(gate, r"['\"]!" + word + r"\b|" + word + r"\(")

    def test_the_dashboards_console_refuses_a_transaction(self):
        for line in ("setbegin", "setcommit", "served begin", "folders begin", "onconnect begin",
                     "onconnect line 1 MODE %nick% +x"):
            with self.subTest(line=line), contextlib.redirect_stdout(io.StringIO()):
                status, result = webserver.build_console_command_result(line)
            self.assertEqual(status, 200)
            self.assertIn("needs a DCC CHAT console", " ".join(result["lines"]))


class TheyAreListedAndDocumented(DCCoreTestCase):

    def test_each_is_in_the_command_table_and_help(self):
        session = make_session(self, structured=False)
        out = "\n".join(run(session, "help"))
        for name in NEW_COMMANDS:
            with self.subTest(name=name):
                self.assertIn(name, adminchat.COMMANDS)
                self.assertRegex(out, r"\n  " + name + r"\b")

    def test_each_is_in_the_guide_with_its_lines(self):
        guide = source("docs", "ADMIN-CONSOLE.md")
        for name in NEW_COMMANDS:
            with self.subTest(name=name):
                self.assertRegex(guide, r"\| `" + name + r"\b")
        for line in ("SETBEGIN", "SETF", "SETEND", "SETOPEN", "SETERR", "SETDONE", "PVBEGIN", "PVLINE", "PVEND",
                     "SRVBEGIN", "SRVLIST", "SRVCHAN", "SRVFOLDER", "SRVEND", "SRVOPEN", "SRVERR", "SRVDONE",
                     "FLDBEGIN", "FLDROW", "FLDEND", "FLDOPEN", "FLDERR", "FLDDONE",
                     "OCBEGIN", "OCLINE", "OCEND", "OCOPEN", "OCERR", "OCDONE", "OCRESEND",
                     "BANBEGIN", "BANP", "BANT", "BANEND", "CAPS"):
            with self.subTest(line=line):
                self.assertIn(f"DCCORE {line}", guide)



if __name__ == "__main__":
    unittest.main()
