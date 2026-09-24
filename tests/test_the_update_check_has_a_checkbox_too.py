"""CHECK_FOR_UPDATES gets a checkbox in dccore.mrc too, not only the dashboard
(follow-up to #572, trialled ahead of merging #913/#914).

The dashboard's Settings page already renders CHECK_FOR_UPDATES as an
ordinary checkbox - it is a plain `bool` setting in defaults.py, and every
bool setting on that page is one. dccore.mrc had no such control: the
"Check for a new version" menu item only ever asks GitHub once, it does
not turn the daily check on or off.

Three pieces, one round trip:

- `checkupdates [on|off]` - a new console command. With no argument it
  reports the setting; `on`/`off` writes it through settings_file.save()
  and a rehash - the same path the dashboard's own Settings save uses -
  and confirms with a DCCORE CHECKUPDATES line, the same one HELLO sends
  so a freshly opened options dialog is never left showing "unknown".
- The options dialog's new checkbox (406) reflects what the bot last
  said, kept in dccore.live (not dccore.ini - it is the bot's state, not
  a local preference) and sends the command only when the checkbox
  actually disagrees with it, so opening and closing the dialog untouched
  triggers no rehash.
- The structured dispatcher's new CHECKUPDATES branch, beside STATUS/PING.
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
        self.set_config(CHECK_FOR_UPDATES=True)
        import commands
        self._real_rehash = commands.handle_rehash_request
        # Not run for real, the same reason and the same way
        # test_web_console.py's test_rehash_returns_only_the_acknowledgement
        # mocks it: the real one reloads config/dcc/announce/security/db/
        # stats_mgr on a background thread this test does not wait for,
        # racing whatever runs next.
        commands.handle_rehash_request = lambda *a, **k: None
        self.addCleanup(setattr, commands, "handle_rehash_request", self._real_rehash)

    def wait_for(self, session, count, timeout=10):
        deadline = time.time() + timeout
        while len(session.lines) < count and time.time() < deadline:
            time.sleep(0.01)
        return session.lines

    def test_it_is_registered(self):
        self.assertIn("checkupdates", adminchat.COMMANDS)

    def structured(self):
        """The mIRC script's session: it said hello, so it reads DCCORE lines."""
        session = FakeSession()
        session.structured = True
        return session

    def test_no_argument_reports_the_current_state(self):
        session = self.structured()
        adminchat.COMMANDS["checkupdates"][0](session, "")

        self.assertEqual(session.lines, ["DCCORE CHECKUPDATES on"])

    def test_no_argument_reports_off_too(self):
        self.set_config(CHECK_FOR_UPDATES=False)
        session = self.structured()
        adminchat.COMMANDS["checkupdates"][0](session, "")

        self.assertEqual(session.lines, ["DCCORE CHECKUPDATES off"])

    def test_a_person_is_answered_in_words(self):
        """A plain DCC chat, or the dashboard's Console: never the protocol
        line, which is for the script."""
        import webserver
        for session in (FakeSession(), webserver._WebConsoleSession("SysOp")):
            adminchat.COMMANDS["checkupdates"][0](session, "")
            self.assertEqual(session.lines, ["The daily update check is on."])
        self.set_config(CHECK_FOR_UPDATES=False)
        session = FakeSession()
        adminchat.COMMANDS["checkupdates"][0](session, "")
        self.assertEqual(session.lines,
                         ["The daily update check is off - checkversion still asks GitHub by hand."])

    def test_a_person_who_turns_it_off_is_told_in_words(self):
        session = FakeSession()
        adminchat.COMMANDS["checkupdates"][0](session, "off")
        lines = self.wait_for(session, 2)
        self.assertNotIn("DCCORE", " ".join(lines))
        self.assertTrue(lines[1].startswith("The daily update check is off"), lines)

    def test_a_bad_argument_is_refused(self):
        session = FakeSession()
        adminchat.COMMANDS["checkupdates"][0](session, "sometimes")

        self.assertEqual(session.lines, ["Usage: checkupdates [on|off]"])

    def test_turning_it_off_writes_the_setting_and_confirms(self):
        session = self.structured()
        adminchat.COMMANDS["checkupdates"][0](session, "off")
        lines = self.wait_for(session, 2)

        self.assertEqual(lines[0], "Turning the daily update check off ...")
        self.assertEqual(lines[1], "DCCORE CHECKUPDATES off")

    def test_turning_it_off_really_writes_the_file(self):
        """Not mocked: settings_file.save() itself, against the redirected
        DCCORE_SETTINGS_FILE every test already writes to."""
        session = FakeSession()
        adminchat.COMMANDS["checkupdates"][0](session, "off")
        self.wait_for(session, 2)

        import settings_file
        with io.open(settings_file.settings_path(), encoding="utf-8") as handle:
            self.assertIn("CHECK_FOR_UPDATES = false", handle.read())

    def test_turning_it_on_confirms_on(self):
        self.set_config(CHECK_FOR_UPDATES=False)
        session = FakeSession()
        session = self.structured()
        adminchat.COMMANDS["checkupdates"][0](session, "ON")
        lines = self.wait_for(session, 2)

        self.assertEqual(lines[1], "DCCORE CHECKUPDATES on")

    def test_a_rehash_is_triggered(self):
        """The write alone is not enough - config.CHECK_FOR_UPDATES only
        actually changes, and version_check.ensure_worker() only actually
        runs, once a rehash reloads it."""
        called = []
        import commands
        commands.handle_rehash_request = lambda *a, **k: called.append(a)

        session = FakeSession()
        adminchat.COMMANDS["checkupdates"][0](session, "on")
        self.wait_for(session, 2)

        self.assertEqual(len(called), 1)


class HelloTellsTheDialogTheState(DCCoreTestCase):
    """So the options dialog is never left showing an unknown state just
    because it opened before the operator ran checkupdates themselves."""

    def setUp(self):
        super().setUp()
        self.set_config(CHECK_FOR_UPDATES=True)

    def test_hello_sends_checkupdates(self):
        session = FakeSession()
        adminchat.COMMANDS["hello"][0](session, "dccore.mrc 1.4")

        self.assertIn("DCCORE CHECKUPDATES on", session.lines)

    def test_hello_sends_off_too(self):
        self.set_config(CHECK_FOR_UPDATES=False)
        session = FakeSession()
        adminchat.COMMANDS["hello"][0](session, "dccore.mrc 1.4")

        self.assertIn("DCCORE CHECKUPDATES off", session.lines)


class TheDialogHasTheCheckbox(unittest.TestCase):
    """mIRC cannot run here - read the same way
    test_the_options_dialog_labels_fit_their_controls.py does: the dialog
    table and the handlers, as source."""

    def dialog_table(self):
        text = script()
        return text.split("dialog dccore.opt {", 1)[1].split("\n}", 1)[0]

    def test_the_checkbox_is_in_the_connection_box(self):
        table = self.dialog_table()
        self.assertIn('check "Check GitHub for a new DCCore version", 406,', table)

    def test_the_control_id_is_not_reused(self):
        """406 must not collide with an existing control in the table."""
        ids = []
        for line in self.dialog_table().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("box ", "title ", "size ", "option ")):
                continue
            parts = stripped.split(",")
            if len(parts) >= 2 and parts[1].strip().isdigit():
                ids.append(int(parts[1].strip()))
        self.assertEqual(len(ids), len(set(ids)), "a control id is reused")
        self.assertIn(406, ids)

    def test_init_reads_the_live_state_not_a_saved_one(self):
        """Not dccore.opt (dccore.ini, a local pref) - dccore.st
        (dccore.live), the bot's own state, refreshed every HELLO."""
        init = script().split("on *:dialog:dccore.opt:init:0: {", 1)[1].split("\n}", 1)[0]
        self.assertIn("$dccore.st(checkupdates)", init)
        self.assertIn("did -c dccore.opt 406", init)

    def test_ok_sends_only_when_the_checkbox_disagrees_with_the_bot(self):
        ok_handler = script().split("on *:dialog:dccore.opt:sclick:1: {", 1)[1].split("\n}", 1)[0]
        self.assertIn("did(dccore.opt,406).state", ok_handler)
        self.assertIn("dccore.st(checkupdates)", ok_handler)
        # Guarded, not unconditional, and on the SAME line: a plain
        # unconditional dccore.send here would rehash the bot every time OK
        # is clicked, whether or not the checkbox actually changed.
        send_lines = [line for line in ok_handler.splitlines() if "dccore.send checkupdates" in line]
        self.assertEqual(len(send_lines), 1, ok_handler)
        self.assertTrue(send_lines[0].strip().startswith("if ("), send_lines[0])
        self.assertIn("checkupdates", send_lines[0].split("dccore.send", 1)[0])

    def test_ok_sends_nothing_before_the_bot_has_said_the_state(self):
        """Before the first CHECKUPDATES line the state is empty and the box
        unticked - and empty is not "off", so OK in that moment used to send
        `checkupdates off`. The send's own condition must require a state."""
        ok_handler = script().split("on *:dialog:dccore.opt:sclick:1: {", 1)[1].split("\n}", 1)[0]
        send_line = [line for line in ok_handler.splitlines() if "dccore.send checkupdates" in line][0]
        condition = send_line.split("{", 1)[0]
        self.assertIn("$dccore.st(checkupdates) != $null", condition)

    def test_the_checkbox_is_never_saved_locally(self):
        """checkupdates must not appear in the `hadd dccore ...` lines
        sclick:1 writes into the persisted dccore.ini hash - only in the
        dccore.live one, which is never saved (see the UNLOAD handler)."""
        ok_handler = script().split("on *:dialog:dccore.opt:sclick:1: {", 1)[1].split("\n}", 1)[0]
        for line in ok_handler.splitlines():
            if line.strip().startswith("hadd dccore "):
                self.assertNotIn("checkupdates", line)


class TheStructuredDispatcherKnowsTheType(unittest.TestCase):

    def dispatcher(self):
        text = script()
        return text.split("alias dccore.structured {", 1)[1].split("\n}\n", 1)[0]

    def test_checkupdates_updates_dccore_live(self):
        body = self.dispatcher()
        self.assertIn("%type == CHECKUPDATES", body)
        branch = body.split("%type == CHECKUPDATES", 1)[1].split("\n", 3)[0:3]
        joined = "\n".join(branch)
        self.assertIn("hadd dccore.live checkupdates", joined)

    def test_it_does_not_touch_the_persisted_hash(self):
        """The same distinction as the dialog side: CHECKUPDATES is the
        bot's live state, never written to dccore.ini."""
        body = self.dispatcher()
        branch_start = body.index("%type == CHECKUPDATES")
        branch = body[branch_start:branch_start + 200]
        self.assertNotIn("dccore.set", branch)
        self.assertNotIn("hadd dccore ", branch.replace("hadd dccore.live", ""))


class TheVersionIsBumped(unittest.TestCase):

    def test_dccore_ver_moved_on_from_913(self):
        text = script()
        # 1.4 for the checkbox, 1.5 for its guard before the bot has spoken.
        self.assertIn("alias dccore.ver { return 1.5 }", text)


class TheMenuHasAToggleToo(unittest.TestCase):
    """test_the_mirc_menu_has_every_command.py requires every non-plumbing
    console command to be in the menu - a toggle earns the same reflective
    label the existing Panel/Connect items already use, reading the bot's
    live state rather than a local pref."""

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
        self.assertIn("$dccore.st(checkupdates)", menu)
        self.assertIn("dccore.send checkupdates", menu)

    def test_it_sits_beside_the_related_check_now_item(self):
        menu = self.menu()
        self.assertLess(menu.index("Check for a new version"), menu.index("checkupdates"))


if __name__ == "__main__":
    unittest.main()
