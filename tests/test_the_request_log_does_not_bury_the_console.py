"""The dashboard's own heartbeat does not bury the operator's console.

From the beta, pasting a screen of this:

    127.0.0.1 - - [07/Sep/2026 22:28:50] "GET /api/fetch/status HTTP/1.1" 200 -
    127.0.0.1 - - [07/Sep/2026 22:28:50] "GET /api/filelists/bots HTTP/1.1" 200 -
    127.0.0.1 - - [07/Sep/2026 22:28:50] "GET /api/console/log?since=33 HTTP/1.1" 200 -

with: "maybe those lines shouldnt be visible on cmd.exe except you run dccore
on something like debug mode. you miss the important lines like search results
etc".

Exactly that. The dashboard polls five endpoints every couple of seconds, so
an idle bot with one page open writes on the order of a hundred lines a
minute. Every one says the same thing - the dashboard is still open - and
together they push a search result, a transfer or a disconnect off the screen
faster than anyone can read them. The console is the operator's only view on a
daemon with no window.

SILENCED, NOT REDIRECTED. Every request werkzeug reports is one this process
just served itself, so nothing is lost that was ever news. ERROR is left
through, so a genuine failure inside the server still reaches the console -
and DEBUG_MODE turns the whole thing back on for anyone who wants it.
"""

import io
import logging
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheRequestLogIsQuietedUnlessAsked(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        logger = logging.getLogger("werkzeug")
        previous = logger.level
        self.addCleanup(logger.setLevel, previous)
        logger.setLevel(logging.NOTSET)

    def level(self):
        return logging.getLogger("werkzeug").level

    def test_an_ordinary_run_stops_the_per_request_lines(self):
        self.set_config(DEBUG_MODE=False)

        webserver._quiet_the_request_log()

        self.assertGreater(self.level(), logging.INFO,
                           "werkzeug still logs a line per request, which is "
                           "what buried the console")

    def test_a_real_server_error_still_gets_through(self):
        """Quieting the heartbeat must not silence a genuine failure inside
        the dashboard - that is a thing the operator needs to see."""
        self.set_config(DEBUG_MODE=False)

        webserver._quiet_the_request_log()

        self.assertLessEqual(self.level(), logging.ERROR)

    def test_debug_mode_leaves_it_alone(self):
        """Someone who has switched debug on is asking for exactly this."""
        self.set_config(DEBUG_MODE=True)

        webserver._quiet_the_request_log()

        self.assertEqual(self.level(), logging.NOTSET)

    def test_it_never_raises(self):
        """A logging tweak is not a reason for the dashboard not to start."""
        self.set_config(DEBUG_MODE=False)
        real = logging.getLogger

        def exploding(name):
            raise RuntimeError("no logging for you")

        logging.getLogger = exploding
        self.addCleanup(setattr, logging, "getLogger", real)

        webserver._quiet_the_request_log()


class ItRunsBeforeTheServerDoes(unittest.TestCase):
    """Called after the app is built and before app.run(), which is the only
    window where it has any effect - werkzeug installs its handler when the
    server starts."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "webserver.py"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_it_is_called_on_the_startup_path(self):
        self.assertIn("_quiet_the_request_log()", self.source())

    def test_it_is_called_before_app_run(self):
        source = self.source()
        called_at = source.index("    _quiet_the_request_log()")
        run_at = source.index("app.run(host=host, port=port")

        self.assertLess(called_at, run_at)


if __name__ == "__main__":
    unittest.main()
