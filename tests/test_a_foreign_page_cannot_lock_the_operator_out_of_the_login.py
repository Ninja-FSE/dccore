"""A page on another site cannot lock the operator out of the dashboard
login (#609).

POST /login is the one route outside the session gate, and its failed-attempt
tracker is keyed on the caller's address. On the stock loopback install the
operator's browser and any hostile page open in that same browser both arrive
as 127.0.0.1, so three cross-site POSTs with a wrong password used to block
the operator's own login for fifteen minutes, repeatable for ever. The route
now refuses a POST whose Origin (or, failing that, Referer) names another
site, before the password is looked at and without counting it.

The reproduction is the audit's own: a cookieless client with a foreign
Origin fails three times, then the operator logs in with the right password.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import webserver  # noqa: E402

from tests.test_every_route_is_behind_the_login import PASSWORD, RouteCase  # noqa: E402

FOREIGN = {"Origin": "http://evil.example", "Referer": "http://evil.example/lure.html"}


class AForeignPageCannotSpendTheOperatorsAttempts(RouteCase):

    def setUp(self):
        super().setUp()
        # RouteCase clears the block pool BEFORE each test; the test below that
        # blocks 127.0.0.1 must not leave it blocked for every later module
        # that logs in from the same address (the whole suite does).
        self.addCleanup(webserver._web_bad_ips.clear)

    def foreign_post(self, password="x", **headers):
        return self.app.test_client().post(
            "/login", data={"password": password}, headers=headers or FOREIGN)

    def test_a_cross_site_login_post_is_refused_before_the_password_is_read(self):
        resp = self.foreign_post(password=PASSWORD)

        self.assertEqual(resp.status_code, 403)
        self.assertIn(b"another site", resp.data)
        self.assertNotIn(b"Incorrect password", resp.data)

    def test_a_refused_cross_site_post_does_not_count_as_a_failed_attempt(self):
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS + 1):
            self.foreign_post()

        self.assertEqual(webserver._web_bad_ips, {})

    def test_the_operator_still_logs_in_after_a_foreign_page_tried_three_times(self):
        """The audit's reproduction, end to end."""
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.foreign_post()

        self.log_in()
        self.assertEqual(self.client.get("/api/queue").status_code, 200)

    def test_a_referer_alone_from_another_site_is_refused_too(self):
        """An older browser, or a strict Origin policy, sends only Referer."""
        resp = self.foreign_post(Referer="http://evil.example/lure.html")

        self.assertEqual(resp.status_code, 403)

    def test_a_null_origin_is_refused(self):
        """What a sandboxed frame or a data: page sends; it is not the
        dashboard's own page."""
        resp = self.foreign_post(Origin="null")

        self.assertEqual(resp.status_code, 403)

    def test_a_wrong_password_from_a_foreign_page_is_not_reported_as_wrong(self):
        """The refusal must not double as a password oracle: right or wrong,
        the answer to another site is the same."""
        wrong = self.foreign_post(password="x")
        right = self.foreign_post(password=PASSWORD)

        self.assertEqual((wrong.status_code, wrong.data), (right.status_code, right.data))


class TheOperatorsOwnBrowserIsUntouched(RouteCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(webserver._web_bad_ips.clear)
    """Controls: the refusal must not catch the dashboard's own form."""

    def own_post(self, password, **headers):
        return self.client.post("/login", data={"password": password}, headers=headers)

    def test_the_dashboards_own_origin_logs_in(self):
        """A browser sends Origin on every POST, its own form's included."""
        host = "127.0.0.1:8420"
        resp = self.client.post("/login", data={"password": PASSWORD},
                                headers={"Origin": "http://" + host,
                                         "Referer": "http://" + host + "/login"},
                                base_url="http://" + host)

        self.assertEqual(resp.status_code, 302)

    def test_the_hostname_comparison_ignores_case_and_a_default_port(self):
        resp = self.client.post("/login", data={"password": PASSWORD},
                                headers={"Origin": "http://Dashboard.Example:80"},
                                base_url="http://dashboard.example")

        self.assertEqual(resp.status_code, 302)

    def test_a_post_with_neither_header_is_still_answered(self):
        """curl, a script, the test client: not a browser forwarding another
        site's form. The guard is against the lockout, not a second password -
        a wrong password here is still counted and refused as before."""
        resp = self.own_post("not-it")

        self.assertEqual(resp.status_code, 401)
        self.assertIn(b"Incorrect password", resp.data)
        self.assertIn("127.0.0.1", webserver._web_bad_ips)

    def test_the_operators_own_repeated_failures_still_block(self):
        """Control on the control: the tracker itself is not weakened."""
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.own_post("not-it", Origin="http://localhost")

        resp = self.own_post(PASSWORD, Origin="http://localhost")

        self.assertEqual(resp.status_code, 401)
        self.assertIn(b"Too many failed attempts", resp.data)


class TheComparisonItself(unittest.TestCase):

    def test_origin_wins_over_referer_when_both_are_present(self):
        self.assertFalse(webserver._login_origin_ok(
            "http://evil.example", "http://127.0.0.1:8420/login", "127.0.0.1:8420"))
        self.assertTrue(webserver._login_origin_ok(
            "http://127.0.0.1:8420", "http://evil.example/", "127.0.0.1:8420"))

    def test_an_ipv6_host_matches_itself(self):
        self.assertTrue(webserver._login_origin_ok("http://[::1]:8420", None, "[::1]:8420"))

    def test_a_different_port_on_the_same_host_is_another_site(self):
        self.assertFalse(webserver._login_origin_ok("http://127.0.0.1:8421", None, "127.0.0.1:8420"))

    def test_a_referer_is_compared_by_its_host_not_its_path(self):
        self.assertTrue(webserver._login_origin_ok(
            None, "http://127.0.0.1:8420/login?lang=fr", "127.0.0.1:8420"))


if __name__ == "__main__":
    unittest.main()
