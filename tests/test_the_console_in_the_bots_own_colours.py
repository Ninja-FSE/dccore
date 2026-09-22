"""#550, step 1: the admin DCC chat shows the feed in the bot's own colours.

A DCC CHAT window is an IRC client and renders mIRC colour codes the way a
channel does. The channel line had a coloured tag per category from the
start; the chat sink stripped every code with the note "a console is read
as a log, not rendered by an IRC client" - true of the dashboard's Console
page, whose sink keeps stripping, and false of this one.

The tag's label and colour now come from ONE table, announce.category_tag(),
read by both the channel line and the console - so a [SECURITY] in the
channel is a [SECURITY] in the console, in the same alert colour, in the
operator's own theme. ADMIN_CHAT_COLOURS (on) turns it off for a client
that shows the codes as junk.
"""

import io
import os
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import defaults as config  # noqa: E402
import theme  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class OneTableForBoth(DCCoreTestCase):
    def palette(self):
        _b, _s, _x, _r, _bold, value, alert, _a = theme.blocks()
        return value, alert

    def test_the_feed_categories_take_the_sent_colour(self):
        value, _alert = self.palette()
        for cat in ("SENT", "REQUEST", "QUEUED", "SENDING", "RESUMED", "SEARCH"):
            with self.subTest(category=cat):
                self.assertEqual(announce.category_tag(cat, theme.blocks()), (cat, value))

    def test_the_alerts(self):
        _value, alert = self.palette()
        self.assertEqual(announce.category_tag("FAIL", theme.blocks()), ("FAIL", alert))
        self.assertEqual(announce.category_tag("PART", theme.blocks()), ("PART", alert))

    def test_the_two_bans_must_not_look_alike(self):
        """BAN is an admin confirming a !ban; HARDBAN is dcc.py's blocked
        path traversal. One is routine, the other is somebody probing."""
        self.assertEqual(announce.category_tag("BAN", theme.blocks())[0], "HARDBAN")
        self.assertEqual(announce.category_tag("HARDBAN", theme.blocks())[0], "SECURITY")

    def test_mute_and_temp_ban_have_their_own_labels(self):
        self.assertEqual(announce.category_tag("MUTE", theme.blocks())[0], "MUTED")
        self.assertEqual(announce.category_tag("TBAN", theme.blocks())[0], "TEMPBAN")

    def test_join_and_quit(self):
        self.assertEqual(announce.category_tag("JOIN", theme.blocks()), ("JOIN", config.C_CYAN))
        self.assertEqual(announce.category_tag("QUIT", theme.blocks()), ("QUIT", config.C_PURPLE))

    def test_anything_else_is_grey_info(self):
        for cat in ("INFO", "weird", "", None):
            with self.subTest(category=cat):
                self.assertEqual(announce.category_tag(cat, theme.blocks()), ("INFO", config.C_GREY))

    def test_case_does_not_matter(self):
        self.assertEqual(announce.category_tag("sent", theme.blocks()), announce.category_tag("SENT", theme.blocks()))

    def test_it_follows_the_theme(self):
        """The value colour differs between presets; the tag must too."""
        self.set_config(THEME="classic")
        classic = announce.category_tag("SENT", theme.blocks())[1]
        self.set_config(THEME="midnight")
        midnight = announce.category_tag("SENT", theme.blocks())[1]
        self.assertNotEqual(classic, midnight)


class TheChannelLineUsesIt(DCCoreTestCase):
    """send_debug()'s tag block reads the same table, so the two can never
    disagree - checked by driving send_debug and reading the queued line."""

    def setUp(self):
        super().setUp()
        # DEBUG_CHANNEL_FEED on, so the feed-only categories (REQUEST...)
        # reach the channel queue too - see announce.channel_wants().
        self.set_config(DEBUG_CHANNEL="#dccore-debug", DEBUG_TO_CHANNEL=True,
                        DEBUG_TO_CONSOLE=False, DEBUG_CHANNEL_FEED=True)
        announce._debug_queue.clear()
        self._real_drain = announce._ensure_debug_drain
        announce._ensure_debug_drain = lambda: None
        self.addCleanup(setattr, announce, "_ensure_debug_drain", self._real_drain)

    def channel_line(self, category):
        announce._debug_queue.clear()
        announce.send_debug("x", category=category)
        return announce._debug_queue[0]

    def test_the_tag_in_the_channel_is_the_tables(self):
        for cat in ("SENT", "FAIL", "BAN", "HARDBAN", "MUTE", "TBAN", "JOIN", "QUIT", "INFO", "REQUEST"):
            with self.subTest(category=cat):
                label, colour = announce.category_tag(cat, theme.blocks())
                self.assertIn(f"{colour}[{label}]", self.channel_line(cat))

    def test_the_old_chain_is_gone(self):
        with io.open(os.path.join(REPO_ROOT, "announce.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def send_debug("):]
        self.assertNotIn('category.upper() == "FAIL"', body, "a second copy of the table is back")
        self.assertIn("label, colour = category_tag(category, (", body)


class TheConsoleLine(DCCoreTestCase):
    def test_coloured_by_default(self):
        line = adminchat.console_line('Sent: "x" to dave', "SENT")
        label, colour = announce.category_tag("SENT", theme.blocks())
        self.assertEqual(line, f'{colour}[{label}]{config.C_RESET} Sent: "x" to dave')

    def test_the_label_is_the_tables_not_the_category(self):
        self.assertTrue(adminchat.console_line("x", "HARDBAN").find("[SECURITY]") >= 0)

    def test_a_callers_bold_is_kept_when_coloured(self):
        """A nick a caller bolded is bolded on a client that renders it."""
        line = adminchat.console_line(f"{config.C_BOLD}dave{config.C_RESET} returned", "JOIN")
        self.assertIn(config.C_BOLD, line)

    def test_off_is_the_plain_console(self):
        self.set_config(ADMIN_CHAT_COLOURS=False)
        line = adminchat.console_line(f"{config.C_BOLD}dave{config.C_RESET} returned", "JOIN")
        self.assertEqual(line, "[JOIN] dave returned")

    def test_off_uses_the_category_verbatim_as_before(self):
        """Pre-#550 the console printed the CATEGORY, so HARDBAN read
        [HARDBAN]; off keeps that, so an existing log parser is unchanged."""
        self.set_config(ADMIN_CHAT_COLOURS=False)
        self.assertEqual(adminchat.console_line("x", "HARDBAN"), "[HARDBAN] x")

    def test_the_sink_goes_through_it(self):
        session = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(session.close, None)
        session.authenticated = True
        session.debug_sink("x", "SENT")
        self.assertEqual(session._outbox[-1], adminchat.console_line("x", "SENT"))

    def test_the_dashboard_sink_still_strips(self):
        """The note that was wrong for the chat is right for the browser."""
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def _console_debug_sink("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("strip_irc_formatting", body)


class TheSettingExists(DCCoreTestCase):
    def test_it_ships_on_in_the_admin_console_category(self):
        import webserver
        self.assertIs(config.ADMIN_CHAT_COLOURS, True)
        payload = webserver.build_settings_payload()
        cat = next(c for c in payload["categories"] if c["id"] == "admin-console")
        field = next(f for f in cat["fields"] if f["name"] == "ADMIN_CHAT_COLOURS")
        self.assertEqual(field["type"], "bool")
        self.assertTrue(field.get("help"))


if __name__ == "__main__":
    unittest.main()
