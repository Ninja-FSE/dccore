"""The dashboard's body-less POST routes could be driven by a form on any
other port of the same host (audit L8, #672).

SameSite=Lax and the JSON content-type were the only CSRF defences, and
seven mutating routes take no JSON body at all: /api/tools/update-list,
/api/filelists/purge-offline, /api/filelists/sources/<nick>/remove,
/api/filelists/<source>/purge, /api/fetch/<id>/delete, /api/messages/read
and /api/notices/read. "Site" does not include the port, so a plain HTML
form auto-submitted on http://127.0.0.1:9000 (a dev server, a NAS or media
UI that renders attacker-influenced HTML) was sent to
http://127.0.0.1:8420 with the operator's session cookie attached: every
offline bot's fetched lists purged, a full master-list rebuild started.

Every POST is now checked against its own Host the way the login's has
been since #609 - Origin, or Referer for an older browser, must name the
dashboard's own host and port - in the same before_request hook that
requires the login, so a route added later is covered without knowing it.
A request with neither header (curl, a script, the test client) is not a
page forwarding another site's form and passes, as at the login.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import webserver  # noqa: E402

from tests.test_every_route_is_behind_the_login import RouteCase  # noqa: E402

# The test client's own host; the session cookie it holds is for this one.
HOST = "localhost"
OTHER_PORT = {"Origin": "http://localhost:9000", "Referer": "http://localhost:9000/evil.html",
              "Sec-Fetch-Site": "same-site"}
OWN = {"Origin": "http://" + HOST, "Referer": "http://" + HOST + "/"}

# The audit's seven, exactly as it listed them.
BODYLESS = ["/api/tools/update-list", "/api/filelists/purge-offline",
            "/api/filelists/sources/somebot/remove", "/api/filelists/somebot/purge",
            "/api/fetch/some-id/delete", "/api/messages/read", "/api/notices/read"]


class AFormOnAnotherLocalPort(RouteCase):

    def setUp(self):
        super().setUp()
        self.log_in()
        self.addCleanup(webserver._web_bad_ips.clear)

    def forged(self, path, **headers):
        return self.client.post(path, data={"x": "y"},
                                headers=headers or OTHER_PORT)

    def test_each_bodyless_route_is_refused_before_it_does_anything(self):
        for path in BODYLESS:
            resp = self.forged(path)

            self.assertEqual(resp.status_code, 403, path)
            self.assertIn("another site", resp.get_json()["error"], path)

    def test_the_audits_probe_does_not_start_a_rebuild(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resp = self.forged("/api/tools/update-list")

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("MAINTENANCE START", out.getvalue())

    def test_a_referer_alone_from_another_port_is_refused(self):
        resp = self.forged("/api/notices/read", Referer="http://localhost:9000/evil.html")

        self.assertEqual(resp.status_code, 403)

    def test_a_null_origin_is_refused(self):
        resp = self.forged("/api/notices/read", Origin="null")

        self.assertEqual(resp.status_code, 403)

    def test_every_post_route_is_covered_not_just_the_seven(self):
        """The check is in the before_request hook, so a JSON route and a
        route added later are behind it too."""
        for rule in self.app.url_map.iter_rules():
            if "POST" not in rule.methods or rule.endpoint in ("login", "static"):
                continue
            path = rule.rule
            for arg in rule.arguments:
                path = path.replace("<%s>" % arg, "x").replace("<path:%s>" % arg, "x")
            resp = self.client.post(path, data={"x": "y"}, headers=OTHER_PORT)

            self.assertEqual(resp.status_code, 403, path)


class TheDashboardsOwnPageIsUntouched(RouteCase):

    def setUp(self):
        super().setUp()
        self.log_in()

    def test_the_pages_own_origin_is_answered(self):
        resp = self.client.post("/api/notices/read", headers=OWN)

        self.assertEqual(resp.status_code, 200)

    def test_a_request_with_neither_header_is_answered(self):
        """curl, a script, the test client."""
        resp = self.client.post("/api/notices/read")

        self.assertEqual(resp.status_code, 200)

    def test_a_get_from_another_port_is_not_the_concern(self):
        """Lax sends the cookie on a cross-site top-level GET too, and a GET
        changes nothing here; the guard is on what mutates."""
        resp = self.client.get("/api/queue", headers=OTHER_PORT)

        self.assertEqual(resp.status_code, 200)

    def test_an_anonymous_forged_post_is_still_told_to_log_in_first(self):
        """The order of the two checks: 401 before 403, as before."""
        resp = self.app.test_client().post("/api/notices/read", headers=OTHER_PORT)

        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
