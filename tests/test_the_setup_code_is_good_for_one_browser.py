"""The setup-page code travelled in a URL handed to the OS browser opener,
readable by any other local user via ps (audit L11, #675).

run_setup_until_configured() prints http://127.0.0.1:8420/setup?token=...
and hands it to webbrowser.open(). On Linux that is xdg-open with the URL
in argv, and the browser it starts keeps the URL in its own argv for as
long as it runs - so on a host shared with other users, `ps aux | grep
token=` during the setup window gave a second user the code, and the page
binds 127.0.0.1, which every local user reaches. They could submit the
form first with a password of their own. Single-user desktops, macOS (the
URL goes to osascript over a pipe) and Windows are unaffected.

The code is good for one browser now: the first request that presents it
gets a cookie, and from then on the code is accepted only together with
that cookie. A second browser with the code is refused; if it was somehow
first, the operator's own is, loudly, and told to restart for a new code.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import webserver  # noqa: E402

from tests import test_set_it_up_in_the_browser as setup  # noqa: E402

GOOD = setup.GOOD


@unittest.skipUnless(webserver.HAVE_FLASK, setup.NEEDS_FLASK)
class TwoBrowsersOneCode(setup.TheApp):
    """The setup app fixture, with a second, cookie-less client for the
    other local user."""

    def setUp(self):
        super().setUp()
        self.other = self.app.test_client()

    def test_the_first_browser_to_present_the_code_is_given_a_cookie(self):
        resp = self.get("/setup?token=the-token")

        self.assertEqual(resp.status_code, 200)
        cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("dccore-setup=", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_a_second_browser_with_the_code_from_ps_is_refused(self):
        self.get("/setup?token=the-token")

        resp = self.other.get("/setup?token=the-token", headers={"Host": "127.0.0.1:8420"})

        self.assertEqual(resp.status_code, 403)
        self.assertIn(b"already been opened in another browser", resp.data)
        self.assertIn(b"stop DCCore and start it again", resp.data)

    def test_and_cannot_submit_the_form_first(self):
        self.get("/setup?token=the-token")

        resp = self.other.post("/setup", data=dict(GOOD, token="the-token"),
                               headers={"Host": "127.0.0.1:8420"})

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.done, [], "another browser's form was applied")

    def test_the_bound_browser_saves_as_before(self):
        self.get("/setup?token=the-token")

        resp = self.post(dict(GOOD, token="the-token"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.done), 1)

    def test_the_cookie_alone_is_not_the_code(self):
        """The cookie proves which browser; the code still has to be there."""
        self.get("/setup?token=the-token")

        resp = self.get("/setup")

        self.assertEqual(resp.status_code, 403)
        self.assertIn(b"one-time code", resp.data)

    def test_a_wrong_cookie_with_the_right_code_is_refused(self):
        self.get("/setup?token=the-token")
        self.other.set_cookie("dccore-setup", "not-the-one")

        resp = self.other.get("/setup?token=the-token", headers={"Host": "127.0.0.1:8420"})

        self.assertEqual(resp.status_code, 403)

    def test_once_saved_the_saved_page_and_its_poll_need_neither(self):
        self.get("/setup?token=the-token")
        self.post(dict(GOOD, token="the-token"))

        self.assertEqual(self.other.get("/setup", headers={"Host": "127.0.0.1:8420"}).status_code, 200)
        self.assertEqual(self.other.get("/login", headers={"Host": "127.0.0.1:8420"}).status_code, 503)

    def test_the_root_redirect_binds_too(self):
        """The link the launcher opens goes to /setup?token=...; a hand-typed
        127.0.0.1:8420/?token=... is the same first visit."""
        resp = self.get("/?token=the-token")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("dccore-setup=", resp.headers.get("Set-Cookie", ""))
        self.assertEqual(self.other.get("/setup?token=the-token", headers={"Host": "127.0.0.1:8420"}).status_code, 403)


class TheGuideSaysSo(unittest.TestCase):

    def test_install_md_names_the_one_browser_rule_and_the_way_out(self):
        import io
        with io.open(os.path.join(REPO_ROOT, "docs", "INSTALL.md"), encoding="utf-8") as handle:
            guide = handle.read()

        self.assertIn("good for one browser", guide)
        self.assertIn("readable with `ps`", guide)
        self.assertIn("stop DCCore and start it\nagain for a fresh code", guide)


for _name in [n for n in dir(setup.TheApp) if n.startswith("test")]:
    setattr(TwoBrowsersOneCode, _name, None)


if __name__ == "__main__":
    unittest.main()
