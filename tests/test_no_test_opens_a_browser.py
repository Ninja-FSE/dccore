"""No test may open a real browser on the machine running the suite.

WHAT HAPPENED

webserver.start() opens the dashboard in the operator's browser - the feature
asked for as "when it is enabled the website should auto open on the default
browser when you start up the program". Two tests in test_webserver.py drive
start() directly to check the WEBUI_ENABLED and WEBUI_HOST gates.

That class was already careful about the OTHER side effect on the same code
path: it replaces create_app() so a regressed gate cannot bind a real socket,
and says why in as many words - "a test must not be able to start a live
listener because the code it is testing broke". The browser call sits one
line above the bind, and was missed.

The result was a developer whose browser opened http://127.0.0.1:8420/ every
few minutes, on a machine where the bot was not running at all. It was the
test suite, once per full run.

WHY THE GUARD IS IN tests/__init__.py

Two other places were considered. tests/support.py reaches only 90 of the 121
test files, and the class that tripped this is a plain unittest.TestCase that
does not import it. Patching in each test that calls start() fixes today's
two and nothing about the next one. Importing the package is the single thing
every run does, whichever file or class is being run.

Recording rather than dropping keeps the behaviour assertable: the tests below
check that start() still WOULD open the dashboard, which is the actual feature.
"""

import io
import os
import sys
import unittest
import webbrowser
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import tests  # noqa: E402
import webserver  # noqa: E402


class TheGuardIsInstalled(unittest.TestCase):

    def test_importing_the_package_replaces_the_browser_openers(self):
        """All three entry points, not just open(). webbrowser.open_new() and
        open_new_tab() launch a browser exactly as readily, and a future
        caller reaching for either would walk straight past a guard that only
        covered the one this bug happened to use."""
        for name in ("open", "open_new", "open_new_tab"):
            with self.subTest(entry_point=name):
                self.assertIs(getattr(webbrowser, name),
                              tests._record_browser_open)

    def test_the_replacement_records_the_url(self):
        before = len(tests.BROWSER_OPENS)

        webbrowser.open("http://example.invalid/probe")

        self.assertEqual(tests.BROWSER_OPENS[before:],
                         ["http://example.invalid/probe"])

    def test_it_reports_success_like_the_real_one(self):
        """webbrowser.open() returns True when it launched something.
        _open_in_browser() does not branch on that today, but a guard that
        returns None would make it start doing so the moment it did."""
        self.assertTrue(webbrowser.open("http://example.invalid/probe"))


class StartingTheDashboardDoesNotReachTheDesktop(unittest.TestCase):
    """The end-to-end shape of the bug: drive start() the way the gate tests
    do, and confirm the browser call is intercepted rather than performed -
    while still confirming it HAPPENED, because opening the dashboard is the
    feature."""

    TEST_PASSWORD_HASH = "pbkdf2_sha256$1000$00$00"

    def setUp(self):
        self._real_flask = webserver.HAVE_FLASK
        webserver.HAVE_FLASK = True
        self.addCleanup(lambda: setattr(webserver, "HAVE_FLASK", self._real_flask))

        recorded = {}

        class FakeApp:
            def run(self, **kwargs):
                recorded.update(kwargs)

        self.recorded = recorded
        real_create = webserver.create_app
        webserver.create_app = lambda *a, **k: FakeApp()
        self.addCleanup(lambda: setattr(webserver, "create_app", real_create))

        for name, value in (("WEBUI_ENABLED", True),
                            ("ADMIN_PASSWORD_HASH", self.TEST_PASSWORD_HASH),
                            ("WEBUI_HOST", "127.0.0.1"),
                            ("WEBUI_PORT", 8420),
                            ("WEBUI_OPEN_BROWSER", True)):
            self._set(name, value)

        self._before = len(tests.BROWSER_OPENS)

    def _set(self, name, value):
        missing = object()
        previous = getattr(config, name, missing)

        def restore():
            if previous is missing:
                if hasattr(config, name):
                    delattr(config, name)
            else:
                setattr(config, name, previous)

        self.addCleanup(restore)
        setattr(config, name, value)

    def opens(self):
        return tests.BROWSER_OPENS[self._before:]

    def test_it_is_recorded_not_performed(self):
        with redirect_stdout(io.StringIO()):
            webserver.start()

        self.assertEqual(self.opens(), ["http://127.0.0.1:8420/"])

    def test_the_switch_still_turns_it_off(self):
        """Control. If the guard were the only thing standing between the
        suite and the desktop, this test would pass while the setting did
        nothing."""
        self._set("WEBUI_OPEN_BROWSER", False)

        with redirect_stdout(io.StringIO()):
            webserver.start()

        self.assertEqual(self.opens(), [])

    def test_a_lan_bound_dashboard_still_opens_nothing(self):
        """The other half of the production rule, for the same reason: a
        dashboard on the LAN is as likely to be a headless box as a desktop."""
        self._set("WEBUI_HOST", "0.0.0.0")

        with redirect_stdout(io.StringIO()):
            webserver.start()

        self.assertEqual(self.opens(), [])

    def test_the_dashboard_was_actually_started(self):
        """Guard on the guard: every assertion above would also pass if
        start() had returned early and never reached the browser call at
        all."""
        with redirect_stdout(io.StringIO()):
            webserver.start()

        self.assertEqual(self.recorded.get("host"), "127.0.0.1")
        self.assertEqual(self.recorded.get("port"), 8420)


if __name__ == "__main__":
    unittest.main()
