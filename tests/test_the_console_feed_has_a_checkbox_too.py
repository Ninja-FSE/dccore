"""DEBUG_TO_CONSOLE gets a checkbox in dccore.mrc too, and a live warning
that the window would otherwise never give (#1006 follow-up).

Found live: an operator's dccore.mrc window kept showing its STATUS burst -
unconditional once a session is structured and authenticated - while
REQUEST, SENDING, SENT, FAIL and SEARCH stayed silent, because
DEBUG_TO_CONSOLE gates those in announce.feed_event() and nothing said so.
A window that looks alive while quietly missing everything else is worse
than one that says why, so the fix has one piece more than checkupdates
(#572) did, mirrored otherwise exactly:

- `consolefeed [on|off]` - a new console command, on the same
  settings_file.save() + rehash path as checkupdates, confirmed with a
  DCCORE CONSOLEFEED line.
- HELLO sends that line every time, not just once ever, and - only when it
  is off - a plain-text warning too, so an operator who never opens Options
  still learns why the window looks half-alive.
- The options dialog's checkbox (405, replacing what used to be a static
  hint label at the same spot) reflects what the bot last said, the same
  dccore.live-not-dccore.ini split as checkupdates's 406.
- The structured dispatcher's new CONSOLEFEED branch, beside CHECKUPDATES.
"""

import io
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_admin_console_commands import FakeSession  # noqa: E402

SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")


def script():
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        return handle.read()


class TheConsoleCommand(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(DEBUG_TO_CONSOLE=True)
        import commands
        self._real_rehash = commands.handle_rehash_request
        commands.handle_rehash_request = lambda *a, **k: None
        self.addCleanup(setattr, commands, "handle_rehash_request", self._real_rehash)

    def wait_for(self, session, count, timeout=10):
        deadline = time.time() + timeout
        while len(session.lines) < count and time.time() < deadline:
            time.sleep(0.01)
        return session.lines

    def structured(self):
        session = FakeSession()
        session.structured = True
        return session

    def test_it_is_registered(self):
        self.assertIn("consolefeed", adminchat.COMMANDS)

    def test_no_argument_reports_the_current_state(self):
        session = self.structured()
        adminchat.COMMANDS["consolefeed"][0](session, "")

        self.assertEqual(session.lines, ["DCCORE CONSOLEFEED on"])

    def test_no_argument_reports_off_too(self):
        self.set_config(DEBUG_TO_CONSOLE=False)
        session = self.structured()
        adminchat.COMMANDS["consolefeed"][0](session, "")

        self.assertEqual(session.lines, ["DCCORE CONSOLEFEED off"])

    def test_a_person_is_answered_in_words(self):
        import webserver
        for session in (FakeSession(), webserver._WebConsoleSession("SysOp")):
            adminchat.COMMANDS["consolefeed"][0](session, "")
            self.assertEqual(session.lines, ["The console feed is on."])
        self.set_config(DEBUG_TO_CONSOLE=False)
        session = FakeSession()
        adminchat.COMMANDS["consolefeed"][0](session, "")
        self.assertEqual(session.lines,
                         ["The console feed is off - status still arrives, nothing else does."])

    def test_a_bad_argument_is_refused(self):
        session = FakeSession()
        adminchat.COMMANDS["consolefeed"][0](session, "sometimes")

        self.assertEqual(session.lines, ["Usage: consolefeed [on|off]"])

    def test_turning_it_off_writes_the_setting_and_confirms(self):
        session = self.structured()
        adminchat.COMMANDS["consolefeed"][0](session, "off")
        lines = self.wait_for(session, 2)

        self.assertEqual(lines[0], "Turning the console feed off ...")
        self.assertEqual(lines[1], "DCCORE CONSOLEFEED off")

    def test_turning_it_off_really_writes_the_file(self):
        session = FakeSession()
        adminchat.COMMANDS["consolefeed"][0](session, "off")
        self.wait_for(session, 2)

        import settings_file
        with io.open(settings_file.settings_path(), encoding="utf-8") as handle:
            self.assertIn("DEBUG_TO_CONSOLE = false", handle.read())

    def test_turning_it_on_confirms_on(self):
        self.set_config(DEBUG_TO_CONSOLE=False)
        session = self.structured()
        adminchat.COMMANDS["consolefeed"][0](session, "ON")
        lines = self.wait_for(session, 2)

        self.assertEqual(lines[1], "DCCORE CONSOLEFEED on")

    def test_a_rehash_is_triggered(self):
        called = []
        import commands
        commands.handle_rehash_request = lambda *a, **k: called.append(a)

        session = FakeSession()
        adminchat.COMMANDS["consolefeed"][0](session, "on")
        self.wait_for(session, 2)

        self.assertEqual(len(called), 1)


class HelloTellsTheDialogTheState(DCCoreTestCase):
    """Unlike checkupdates, said again every hello - not just left for the
    dialog to notice - because a window that only ever shows STATUS looks
    alive, not silenced."""

    def setUp(self):
        super().setUp()
        self.set_config(DEBUG_TO_CONSOLE=True)

    def test_hello_sends_consolefeed(self):
        session = FakeSession()
        adminchat.COMMANDS["hello"][0](session, "dccore.mrc 1.7")

        self.assertIn("DCCORE CONSOLEFEED on", session.lines)

    def test_hello_sends_off_too(self):
        self.set_config(DEBUG_TO_CONSOLE=False)
        session = FakeSession()
        adminchat.COMMANDS["hello"][0](session, "dccore.mrc 1.7")

        self.assertIn("DCCORE CONSOLEFEED off", session.lines)

    def test_on_says_nothing_extra(self):
        """The plain warning is for the off case alone - an operator who
        already has the feed does not need to be told so in prose too."""
        session = FakeSession()
        adminchat.COMMANDS["hello"][0](session, "dccore.mrc 1.7")

        self.assertFalse(any("console feed is off" in line.lower() for line in session.lines))

    def test_off_also_gets_a_plain_warning(self):
        """The one thing a checkbox nobody has opened Options to see cannot
        do: reach an operator who has never opened it."""
        self.set_config(DEBUG_TO_CONSOLE=False)
        session = FakeSession()
        adminchat.COMMANDS["hello"][0](session, "dccore.mrc 1.7")

        warnings = [line for line in session.lines if "console feed is off" in line.lower()]
        self.assertEqual(len(warnings), 1, session.lines)
        self.assertNotIn("DCCORE", warnings[0])

    def test_the_warning_names_the_real_dashboard_label(self):
        """The #1009 review: it used to say "Settings > Console
        feed", a different category (CONSOLE_SHOW_* and DEBUG_CHANNEL_FEED
        live there) - DEBUG_TO_CONSOLE is under Debug & logging. Read from
        en.json rather than hardcoded here, so the two cannot drift apart
        again without this failing."""
        with io.open(os.path.join(REPO_ROOT, "web", "lang", "en.json"), encoding="utf-8") as handle:
            import json
            label = json.load(handle)["settings.field.DEBUG_TO_CONSOLE"]

        self.set_config(DEBUG_TO_CONSOLE=False)
        session = FakeSession()
        adminchat.COMMANDS["hello"][0](session, "dccore.mrc 1.7")

        warning = [line for line in session.lines if "console feed is off" in line.lower()][0]
        self.assertIn("Debug & logging", warning)
        self.assertIn(label, warning)

    def test_the_warning_names_a_command_that_actually_exists(self):
        """The #1009 review: it used to say "/dccore consolefeed
        on", which fell through to the help text - dccore.mrc's `alias
        dccore` had no %cmd branch for it."""
        with io.open(SCRIPT, encoding="ascii", newline="") as handle:
            mrc = handle.read()
        alias_body = mrc.split("alias dccore {", 1)[1].split("\nalias ", 1)[0]
        self.assertIn("%cmd == consolefeed", alias_body)

        session = FakeSession()
        self.set_config(DEBUG_TO_CONSOLE=False)
        adminchat.COMMANDS["hello"][0](session, "dccore.mrc 1.7")
        warning = [line for line in session.lines if "console feed is off" in line.lower()][0]
        self.assertIn("/dccore consolefeed on", warning)


class TheDialogHasTheCheckbox(unittest.TestCase):
    """mIRC cannot run here - read the same way
    test_the_options_dialog_labels_fit_their_controls.py does: the dialog
    table and the handlers, as source."""

    def dialog_table(self):
        text = script()
        return text.split("dialog dccore.opt {", 1)[1].split("\n}", 1)[0]

    def test_the_checkbox_is_in_the_connection_box(self):
        table = self.dialog_table()
        self.assertIn('check "Console feed on (also requests and sends - not just status)", 405,', table)

    def test_the_control_id_is_not_reused(self):
        ids = []
        for line in self.dialog_table().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("box ", "title ", "size ", "option ")):
                continue
            parts = stripped.split(",")
            if len(parts) >= 2 and parts[1].strip().isdigit():
                ids.append(int(parts[1].strip()))
        self.assertEqual(len(ids), len(set(ids)), "a control id is reused")
        self.assertIn(405, ids)

    def test_init_reads_the_live_state_not_a_saved_one(self):
        init = script().split("on *:dialog:dccore.opt:init:0: {", 1)[1].split("\n}", 1)[0]
        self.assertIn("$dccore.st(consolefeed)", init)
        self.assertIn("did -c dccore.opt 405", init)

    def test_ok_sends_only_when_the_checkbox_disagrees_with_the_bot(self):
        ok_handler = script().split("on *:dialog:dccore.opt:sclick:1: {", 1)[1].split("\n}", 1)[0]
        self.assertIn("did(dccore.opt,405).state", ok_handler)
        self.assertIn("dccore.st(consolefeed)", ok_handler)
        send_lines = [line for line in ok_handler.splitlines() if "dccore.send consolefeed" in line]
        self.assertEqual(len(send_lines), 1, ok_handler)
        self.assertTrue(send_lines[0].strip().startswith("if ("), send_lines[0])
        self.assertIn("consolefeed", send_lines[0].split("dccore.send", 1)[0])

    def test_ok_sends_nothing_before_the_bot_has_said_the_state(self):
        ok_handler = script().split("on *:dialog:dccore.opt:sclick:1: {", 1)[1].split("\n}", 1)[0]
        send_line = [line for line in ok_handler.splitlines() if "dccore.send consolefeed" in line][0]
        condition = send_line.split("{", 1)[0]
        self.assertIn("$dccore.st(consolefeed) != $null", condition)

    def test_the_checkbox_is_never_saved_locally(self):
        ok_handler = script().split("on *:dialog:dccore.opt:sclick:1: {", 1)[1].split("\n}", 1)[0]
        for line in ok_handler.splitlines():
            if line.strip().startswith("hadd dccore "):
                self.assertNotIn("consolefeed", line)


class TheStructuredDispatcherKnowsTheType(unittest.TestCase):

    def dispatcher(self):
        text = script()
        return text.split("alias dccore.structured {", 1)[1].split("\n}\n", 1)[0]

    def test_consolefeed_updates_dccore_live(self):
        body = self.dispatcher()
        self.assertIn("%type == CONSOLEFEED", body)
        branch_start = body.index("%type == CONSOLEFEED")
        branch = body[branch_start:branch_start + 300]
        self.assertIn("hadd dccore.live consolefeed", branch)

    def test_it_does_not_touch_the_persisted_hash(self):
        body = self.dispatcher()
        branch_start = body.index("%type == CONSOLEFEED")
        branch = body[branch_start:branch_start + 300]
        end = branch.index("return }") + len("return }")
        branch = branch[:end]
        self.assertNotIn("dccore.set", branch)
        self.assertNotIn("hadd dccore ", branch.replace("hadd dccore.live", ""))


class TheMenuHasAToggleToo(unittest.TestCase):

    def menu(self):
        text = script()
        start = text.index("menu @DCCore {")
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        raise AssertionError("menu @DCCore { ... } not closed")

    def test_the_item_reads_the_live_state_and_toggles_it(self):
        menu = self.menu()
        self.assertIn("$dccore.st(consolefeed)", menu)
        self.assertIn("dccore.send consolefeed", menu)

    def test_it_sits_beside_the_related_toggle(self):
        menu = self.menu()
        self.assertLess(menu.index("Daily update check"), menu.index("Console feed"))


if __name__ == "__main__":
    unittest.main()
