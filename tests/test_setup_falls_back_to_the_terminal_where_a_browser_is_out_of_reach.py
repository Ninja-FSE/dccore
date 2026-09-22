"""#595: over SSH, the first run waited for ever for a form nobody could open.

The docs promise that with no browser the setup questions are asked in the
terminal. The code only tested whether Flask was importable: when it was, the
launcher started the daemon to serve the setup page on 127.0.0.1 and waited. On
a box reached over SSH that link is unreachable from the operator's computer,
webbrowser.open() returned False and nobody looked, Ctrl-C ended in a traceback,
nothing on screen said `python3 configure.py`, and because Flask was now
installed every later run took the same road.

Now: in an SSH session the questions are asked in the terminal (unless
DCCORE_SETUP_IN_BROWSER=1 says a tunnel is up); the page says how else to go on
- an ssh tunnel, or Ctrl-C and configure.py - when no browser could be opened;
and Ctrl-C leaves cleanly with that instruction.
"""

import os
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import configure  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_set_it_up_in_the_browser import (  # noqa: E402
    LOOPBACK_OK, NEEDS_FLASK, NEEDS_LOOPBACK, free_port)


class OverSsh(unittest.TestCase):

    def offer(self, environ, ask=None):
        logs, asked = [], []

        def fake_ask(prompt):
            asked.append(prompt)
            return ask if ask is not None else ""

        result = configure.offer_setup_in_browser(ask=fake_ask, log=logs.append, environ=environ)
        return result, "\n".join(logs), asked

    def test_each_ssh_variable_is_recognised(self):
        for name in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT"):
            self.assertTrue(configure.over_ssh({name: "192.0.2.1 22 192.0.2.2 22"}), name)

    def test_no_ssh_variable_is_not_ssh(self):
        self.assertFalse(configure.over_ssh({}))
        self.assertFalse(configure.over_ssh({"SSH_CONNECTION": ""}))
        self.assertFalse(configure.over_ssh({"HOME": "/home/x"}))

    def test_over_ssh_the_questions_are_asked_here_even_with_flask(self):
        result, said, asked = self.offer({"SSH_CONNECTION": "a b c d"})
        self.assertEqual(result, 2)
        self.assertEqual(asked, [], "nothing to install and nothing to ask")

    def test_it_says_why_and_how_to_use_the_browser_anyway(self):
        _, said, _ = self.offer({"SSH_TTY": "/dev/pts/0"})
        self.assertIn("over SSH", said)
        self.assertIn("ssh -L 8420:127.0.0.1:8420", said)
        self.assertIn("DCCORE_SETUP_IN_BROWSER=1", said)

    def test_with_a_tunnel_declared_the_page_is_kept(self):
        if not webserver.HAVE_FLASK:
            raise unittest.SkipTest(NEEDS_FLASK)
        result, _, asked = self.offer({"SSH_CONNECTION": "a b c d", "DCCORE_SETUP_IN_BROWSER": "1"})
        self.assertEqual(result, 0)

    def test_at_the_machine_nothing_changes(self):
        if not webserver.HAVE_FLASK:
            raise unittest.SkipTest(NEEDS_FLASK)
        result, _, asked = self.offer({})
        self.assertEqual(result, 0)
        self.assertEqual(asked, [])

    def test_the_default_environment_is_the_real_one(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "a b c d"}):
            self.assertEqual(configure.offer_setup_in_browser(ask=lambda p: "", log=lambda *_: None), 2)


@unittest.skipUnless(webserver.HAVE_FLASK, NEEDS_FLASK)
@unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
class TheSetupPageTellsYouHowElseToGoOn(DCCoreTestCase):

    def serve(self, opener, wait):
        logs = []
        result = webserver.run_setup_until_configured(port=free_port(), log=logs.append, opener=opener,
                                                      wait=wait, token="tok")
        return result, "\n".join(logs)

    def test_no_browser_opened_says_how_else(self):
        result, said = self.serve(opener=lambda url: False, wait=lambda: True)
        self.assertIsNone(result)
        self.assertIn("No browser was opened here", said)
        self.assertIn("ssh -L", said)
        self.assertIn("127.0.0.1", said)

    def test_a_browser_that_opened_does_not_get_the_tunnel_hint(self):
        _, said = self.serve(opener=lambda url: True, wait=lambda: True)
        self.assertNotIn("No browser was opened here", said)

    def test_the_terminal_way_is_always_named(self):
        for opened in (True, False):
            _, said = self.serve(opener=lambda url, o=opened: o, wait=lambda: True)
            self.assertIn("python3 configure.py", said)

    def test_ctrl_c_leaves_cleanly_and_says_what_to_do(self):
        def interrupted():
            raise KeyboardInterrupt

        result, said = self.serve(opener=lambda url: True, wait=interrupted)
        self.assertIsNone(result)
        self.assertIn("Stopped. Answer the questions here instead: python3 configure.py", said)

    def test_the_port_is_freed_after_ctrl_c(self):
        port = free_port()

        def interrupted():
            raise KeyboardInterrupt

        webserver.run_setup_until_configured(port=port, log=lambda *_: None, opener=lambda u: True,
                                             wait=interrupted, token="tok")
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
