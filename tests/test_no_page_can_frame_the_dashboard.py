"""No other page may show the dashboard inside a frame.

No response said anything about framing. A page on another port of the same
host - a dev server, a NAS interface - is the same SITE to a browser, so the
SameSite=Lax session cookie went with a framed dashboard, and a click lured
onto a framed button (remove from queue, lift an ignore, re-download a list)
arrived from the dashboard's own page, past the Origin check.

Every response now carries X-Frame-Options: DENY and CSP frame-ancestors
'none' - the login page, the refusals and the setup page included.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_every_route_is_behind_the_login import RouteCase  # noqa: E402


class TheHeaders(DCCoreTestCase):
    """_refuse_framing() on a stand-in response - no Flask."""

    class Response:
        def __init__(self, headers=None):
            self.headers = dict(headers or {})

    def test_both_are_set(self):
        response = webserver._refuse_framing(self.Response())

        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Content-Security-Policy"], "frame-ancestors 'none'")

    def test_a_policy_a_route_set_itself_is_kept(self):
        response = webserver._refuse_framing(self.Response(
            {"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'"}))

        self.assertEqual(response.headers["Content-Security-Policy"],
                         "default-src 'self'; frame-ancestors 'none'")


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class EveryResponseRefusesAFrame(RouteCase):

    def assertNotFramable(self, resp):
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", resp.headers.get("Content-Security-Policy", ""))

    def test_the_login_page(self):
        self.assertNotFramable(self.client.get("/login"))

    def test_the_dashboard_and_its_api_when_logged_in(self):
        self.log_in()
        for path in ("/", "/api/queue", "/app.js"):
            with self.subTest(path=path):
                resp = self.client.get(path)
                resp.close()  # a static file is streamed from an open handle
                self.assertEqual(resp.status_code, 200)
                self.assertNotFramable(resp)

    def test_the_refusals(self):
        self.assertNotFramable(self.client.get("/"))            # redirect to the login
        self.assertNotFramable(self.client.get("/api/queue"))   # 401
        self.assertNotFramable(self.client.get("/login", headers={"Host": "elsewhere.example"}))

    def test_the_setup_page(self):
        app = webserver.create_setup_app("the-token", on_done=lambda changes: None)
        resp = app.test_client().get("/setup?token=the-token", headers={"Host": "127.0.0.1:8420"})

        self.assertNotFramable(resp)


if __name__ == "__main__":
    unittest.main()
