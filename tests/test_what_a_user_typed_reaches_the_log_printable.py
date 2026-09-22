"""User-typed request and search text reached the terminal, the debug
channel and the admin chat with IRC formatting and terminal control
characters intact (audit L6, #670).

"!DCCore !rar \x1b]0;pwned\x07\x034,4 SENT: admin.rar to victim" from any
channel member: the artist-root refusal printed the text to stdout (the
operator's Windows Terminal window was retitled) and sent it through
send_debug(), which strips only bold, reset and the mIRC colour byte, so
the debug channel and the colour-rendering admin chat showed a red block
that read like a fake SENT line inside the PART line. A search term took
the same route through execute_search()'s own print and feed_event(). CR
and LF cannot be injected - no IRC command can be forged - so this is
cosmetic and misleading, not a takeover.

The text is cleaned where irc.py takes it off the wire: list.printable_text()
- strip_control_codes() and then every remaining C0 control, DEL and the C1
range - so what the handlers print, log and feed is what the operator sees.
"""

import os
import sys
import threading
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import irc  # noqa: E402
import list as list_mod  # noqa: E402

from tests import test_the_bots_own_nick_follows_the_server as own  # noqa: E402

HOSTILE = "\x1b]0;pwned\x07\x034,4 SENT: admin.rar to victim\x16"
CHANNEL = "#somechannel"


class TheCleaner(unittest.TestCase):

    def test_the_audits_probe_comes_out_as_plain_text(self):
        self.assertEqual(list_mod.printable_text("!rar " + HOSTILE), "!rar ]0;pwned SENT: admin.rar to victim")

    def test_every_control_character_goes(self):
        every_c0 = "".join(chr(n) for n in range(0x20)) + "\x7f" + "".join(chr(n) for n in range(0x80, 0xa0))

        self.assertEqual(list_mod.printable_text("a" + every_c0 + "b"), "ab")

    def test_what_a_real_request_holds_is_untouched(self):
        for text in ["Artist/Album (1991) - Song.flac  ::INFO:: 4KB",
                     "!rar Metallica/Black Album (1991)",
                     "Ünïcödé – dash, nbsp\u00a0kept",
                     "Song [Live] (Remaster) & more"]:
            self.assertEqual(list_mod.printable_text(text), text)

    def test_mirc_formatting_still_goes_the_way_it_did(self):
        self.assertEqual(list_mod.printable_text("\x02bold\x02 \x0312,4colour\x03 \x1funder\x1f \x0freset"),
                         "bold colour under reset")


class _Records:
    """threading.Thread for the read loop: what irc_loop() would have started,
    and with what."""

    started = []

    def __init__(self, target=None, args=(), kwargs=None, **_k):
        self.target, self.args = target, tuple(args)

    def start(self):
        _Records.started.append((self.target, self.args))


class WhatTheHandlersAreHanded(own.DrivesPastRegistration):
    """irc_loop() for real: 001, then a channel line with the audit's probe
    in it, and the thread the loop starts for the handler recorded."""

    def setUp(self):
        super().setUp()
        _Records.started = []
        irc.threading.Thread = _Records

    def handed_to(self, target):
        return [args for t, args in _Records.started if t is target]

    def channel_line(self, text):
        return ":dave!~d@host.example PRIVMSG %s :%s" % (CHANNEL, text)

    def test_a_request_reaches_the_handler_as_plain_text(self):
        self.run_registration(own.NOTICE_AUTH, own.welcome("SomeBot"),
                              self.channel_line("!SomeBot !rar " + HOSTILE))

        handed = self.handed_to(dcc.handle_download_request)
        self.assertEqual(len(handed), 1, _Records.started)
        self.assertEqual(handed[0][1:], ("dave", "!rar ]0;pwned SENT: admin.rar to victim", CHANNEL))

    def test_a_search_reaches_the_handler_as_plain_text(self):
        self.run_registration(own.NOTICE_AUTH, own.welcome("SomeBot"),
                              self.channel_line("@find " + HOSTILE))

        handed = self.handed_to(list_mod.execute_search)
        self.assertEqual(len(handed), 1, _Records.started)
        self.assertEqual(handed[0][1:], ("dave", "]0;pwned SENT: admin.rar to victim", CHANNEL))

    def test_a_search_that_is_nothing_but_control_characters_is_not_run(self):
        self.run_registration(own.NOTICE_AUTH, own.welcome("SomeBot"),
                              self.channel_line("@find \x1b\x07\x16"))

        self.assertEqual(self.handed_to(list_mod.execute_search), [])

    def test_an_ordinary_request_is_handed_over_as_typed(self):
        self.run_registration(own.NOTICE_AUTH, own.welcome("SomeBot"),
                              self.channel_line("!SomeBot Artist/Album (1991) - Song.flac  ::INFO:: 4KB"))

        handed = self.handed_to(dcc.handle_download_request)
        self.assertEqual([a[2] for a in handed], ["Artist/Album (1991) - Song.flac  ::INFO:: 4KB"])


for _name in [n for n in dir(own.DrivesPastRegistration) if n.startswith("test")]:
    setattr(WhatTheHandlersAreHanded, _name, None)


if __name__ == "__main__":
    unittest.main()
