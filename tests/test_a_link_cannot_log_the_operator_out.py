"""/logout accepted GET, so a cross-site link logged the operator out
(audit L9, #673).

A state-changing action on GET: with the session cookie SameSite=Lax, a
top-level navigation from any site - a link click, a redirect - to
http://127.0.0.1:8420/logout carried the cookie and cleared the session;
the dashboard tab's next poll answered 401 and the page dropped to the
login form. A nuisance, not a breach. The page's own button has always
been a POST form, so GET was unused by the app; it is not accepted now.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.test_every_route_is_behind_the_login import RouteCase  # noqa: E402


class ALinkToLogout(RouteCase):

    def setUp(self):
        super().setUp()
        self.log_in()

    def test_a_get_does_not_log_the_operator_out(self):
        """The audit's reproduction: a navigation from another site."""
        resp = self.client.get("/logout", headers={"Referer": "http://evil.example/"})

        # 404, not 405: the static route ("" prefix) answers a GET the
        # logout rule no longer claims. Either way, nothing happened.
        self.assertIn(resp.status_code, (404, 405))
        self.assertEqual(self.client.get("/api/queue").status_code, 200, "the session was cleared")

    def test_head_and_the_rest_are_refused_too(self):
        for method in ("head", "put", "delete"):
            resp = getattr(self.client, method)("/logout")

            self.assertIn(resp.status_code, (404, 405), method)
        self.assertEqual(self.client.get("/api/queue").status_code, 200)

    def test_the_pages_own_form_still_logs_out(self):
        resp = self.client.post("/logout", headers={"Origin": "http://localhost"})

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/login"))
        self.assertEqual(self.client.get("/api/queue").status_code, 401)

    def test_the_route_is_registered_for_post_alone(self):
        rule = [r for r in self.app.url_map.iter_rules() if r.endpoint == "logout"][0]

        self.assertEqual(rule.methods - {"OPTIONS"}, {"POST"})

    def test_the_pages_button_is_a_form_that_posts(self):
        with io.open(os.path.join(REPO_ROOT, "web", "index.html"), encoding="utf-8") as handle:
            page = handle.read()

        self.assertIn('<form method="post" action="/logout">', page)
        self.assertNotIn('href="/logout"', page)


if __name__ == "__main__":
    unittest.main()
