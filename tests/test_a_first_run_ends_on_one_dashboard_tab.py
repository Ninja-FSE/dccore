"""After browser setup with the dashboard on loopback, two browser tabs
opened (audit L25, #689).

run_setup_until_configured() had already put the browser on the setup
page, whose "Saved" screen polls /login and navigates there the moment the
real app answers. start() then called _open_in_browser() with no knowledge
that the setup page had just run and opened http://127.0.0.1:8420/ as well
- a first run ended on two dashboard tabs, one on /login and one on /
(which redirects to /login). Choosing the LAN host or WEBUI_OPEN_BROWSER =
False avoided it, by avoiding the browser altogether.

run_setup_until_configured() now notes when the tab it opened is going to
arrive at the login by itself - a browser was opened and the dashboard was
chosen - and _open_in_browser() stands down once, saying so.
"""

import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import webserver  # noqa: E402

from tests import test_set_it_up_in_the_browser as setup  # noqa: E402

GOOD = setup.GOOD


class TheFlagOnItsOwn(unittest.TestCase):

    def setUp(self):
        self.addCleanup(setattr, webserver, "_browser_is_on_the_saved_page", False)
        from tests.support import reset_config
        reset_config()
        self.addCleanup(reset_config)
        import defaults as config
        config.WEBUI_OPEN_BROWSER = True

    def test_start_does_not_open_a_second_tab_after_the_setup_page(self):
        webserver._browser_is_on_the_saved_page = True
        opened, said = [], []

        result = webserver._open_in_browser("127.0.0.1", 8420, opener=opened.append, log=said.append)

        self.assertFalse(result)
        self.assertEqual(opened, [])
        self.assertIn("opens the login by itself; not opening another", said[0])

    def test_and_stands_down_only_once(self):
        """The next start of the dashboard - a restart, a rehash that turns
        it back on - opens the browser as it always did."""
        webserver._browser_is_on_the_saved_page = True
        opened = []
        webserver._open_in_browser("127.0.0.1", 8420, opener=opened.append, log=lambda *_: None)

        webserver._open_in_browser("127.0.0.1", 8420, opener=opened.append, log=lambda *_: None)

        self.assertEqual(opened, ["http://127.0.0.1:8420/"])
        self.assertFalse(webserver._browser_is_on_the_saved_page)

    def test_without_the_setup_page_it_opens_as_before(self):
        opened = []

        self.assertTrue(webserver._open_in_browser("127.0.0.1", 8420, opener=opened.append))
        self.assertEqual(opened, ["http://127.0.0.1:8420/"])


@unittest.skipUnless(webserver.HAVE_FLASK, setup.NEEDS_FLASK)
class AfterTheRealSetupPage(setup.TheServer):
    """The audit's reproduction: the real setup server, a browser 'opened'
    by the recorder, the form saved with the dashboard on - and then the
    call start() makes."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, webserver, "_browser_is_on_the_saved_page", False)

    def run_setup(self, form):
        port = setup.free_port()
        logs, opened, result = [], [], {}

        def run():
            result["changes"] = webserver.run_setup_until_configured(
                port=port, log=logs.append, opener=lambda url: (opened.append(url), True)[1], token="tok")
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not opened:
            time.sleep(0.02)
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        # Waited for, not bet on: this file runs early in the suite, and on
        # a cold macOS runner the setup server's first answer took longer
        # than one five-second timeout.
        last = None
        for _ in range(12):
            try:
                browser.open(f"http://127.0.0.1:{port}/setup?token=tok", timeout=5).read()
                break
            except (urllib.error.URLError, OSError) as err:
                last = err
                time.sleep(0.5)
        else:
            self.fail("the setup page never answered: %r; log: %r" % (last, logs))
        browser.open(f"http://127.0.0.1:{port}/setup",
                     data=urllib.parse.urlencode(dict(form, token="tok")).encode(), timeout=10).read()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        return opened, logs

    @unittest.skipUnless(setup.LOOPBACK_OK, setup.NEEDS_LOOPBACK)
    def test_with_the_dashboard_chosen_start_opens_no_second_tab(self):
        opened, _logs = self.run_setup(GOOD)
        self.assertEqual(len(opened), 1, "the setup page's own tab")

        result = webserver._open_in_browser("127.0.0.1", 8420, opener=opened.append, log=lambda *_: None)

        self.assertFalse(result)
        self.assertEqual(len(opened), 1, "a second tab was opened: %r" % opened)

    @unittest.skipUnless(setup.LOOPBACK_OK, setup.NEEDS_LOOPBACK)
    def test_with_no_dashboard_nothing_is_arriving_and_nothing_changes(self):
        """No dashboard means the saved page has nowhere to go and start()
        does not start it anyway; the flag must not be left set for the
        next time the dashboard is turned on."""
        self.run_setup({k: v for k, v in GOOD.items() if k != "WEBUI_ENABLED"})

        self.assertFalse(webserver._browser_is_on_the_saved_page)


for _name in [n for n in dir(setup.TheServer) if n.startswith("test")]:
    setattr(AfterTheRealSetupPage, _name, None)


if __name__ == "__main__":
    unittest.main()
