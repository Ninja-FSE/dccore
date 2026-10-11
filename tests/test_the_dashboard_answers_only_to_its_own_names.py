"""The dashboard answers only to names that cannot be another site's.

DNS REBINDING. A page on a site's own name can point that name at 127.0.0.1
after it has loaded. Its requests then reach the dashboard as same-origin:
the login's cross-site guard (#609) compares Origin with Host, and both
carry the site's name. Three wrong passwords from such a page were charged
to 127.0.0.1 - the operator's own address - and the operator's next login,
with the right password, was refused for the whole block window.

The Host header is what the page cannot change, so the dashboard now
refuses any name that is not an IP address, localhost, a name the operator
configured (WEBUI_ALLOWED_HOSTS, or WEBUI_HOST itself) or - on a LAN bind -
this computer's own name. Refused first, before the login reads anything,
and never counted against the address.
"""

import os
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_every_route_is_behind_the_login import PASSWORD, RouteCase  # noqa: E402

REBOUND = "rebound-site.example:8420"
OWN_NAME = "workbench-seven"


class TheNamesItAnswersTo(DCCoreTestCase):
    """_dashboard_host_ok() itself - no Flask."""

    def setUp(self):
        super().setUp()
        self.real_machine_names = webserver._machine_names
        webserver._machine_names = lambda: frozenset({OWN_NAME, OWN_NAME + ".local"})
        self.addCleanup(setattr, webserver, "_machine_names", self.real_machine_names)

    def ok(self, host, bound="127.0.0.1"):
        return webserver._dashboard_host_ok(host, bound=bound)

    def test_any_ip_address_and_localhost(self):
        for host in ("127.0.0.1", "127.0.0.1:8420", "[::1]:8420", "[::1]",
                     "192.168.1.20:8420", "10.0.0.5", "localhost", "LocalHost:8420",
                     "localhost.:8420"):
            with self.subTest(host=host):
                self.assertTrue(self.ok(host))

    def test_another_name_is_refused(self):
        for host in (REBOUND, "rebound-site.example", "localhost.rebound-site.example",
                     "127.0.0.1.rebound-site.example", "", "   "):
            with self.subTest(host=host):
                self.assertFalse(self.ok(host))

    def test_a_configured_name_is_answered_whatever_its_case_and_port(self):
        self.set_config(WEBUI_ALLOWED_HOSTS=["Dashboard.Home.Example", "proxy.example:443"])

        self.assertTrue(self.ok("dashboard.home.example:8420"))
        self.assertTrue(self.ok("PROXY.example"))
        self.assertFalse(self.ok(REBOUND))

    def test_a_configured_name_may_be_a_comma_separated_string(self):
        self.set_config(WEBUI_ALLOWED_HOSTS="one.example, two.example")

        self.assertTrue(self.ok("two.example:8420"))

    def test_a_star_turns_the_check_off(self):
        self.set_config(WEBUI_ALLOWED_HOSTS=["*"])

        self.assertTrue(self.ok(REBOUND))

    def test_webui_host_set_to_a_name_is_answered(self):
        self.set_config(WEBUI_HOST="bot-box.home.example")

        self.assertTrue(self.ok("bot-box.home.example:8420", bound="bot-box.home.example"))

    def test_the_computers_own_name_only_on_a_lan_bind(self):
        """A phone on the LAN opens http://<computer name>:8420/ - but on a
        loopback bind nothing on the LAN reaches the dashboard, and there
        is no reason to answer to it."""
        self.assertTrue(self.ok(OWN_NAME + ":8420", bound="0.0.0.0"))
        self.assertTrue(self.ok(OWN_NAME.upper() + ".local", bound="0.0.0.0"))
        self.assertTrue(self.ok(OWN_NAME, bound=""))
        self.assertFalse(self.ok(OWN_NAME, bound="127.0.0.1"))
        self.assertFalse(self.ok(REBOUND, bound="0.0.0.0"))

    def test_the_real_machine_names_include_its_host_name(self):
        self.addCleanup(webserver._machine_name_cache.clear)
        webserver._machine_name_cache.clear()
        names = self.real_machine_names()

        self.assertIn(socket.gethostname().strip().lower().rstrip("."), names)


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class ARebindingPageCannotLockTheOperatorOut(RouteCase):
    """The audit's reproduction, through the real app."""

    def setUp(self):
        super().setUp()
        self.addCleanup(webserver._web_bad_ips.clear)

    def rebound_post(self, password="wrong"):
        return self.app.test_client().post(
            "/login", data={"password": password},
            headers={"Host": REBOUND, "Origin": "http://" + REBOUND})

    def test_its_login_posts_are_refused_and_not_counted(self):
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            resp = self.rebound_post()
            self.assertEqual(resp.status_code, 403)
            self.assertNotIn(b"Incorrect password", resp.data)

        self.assertEqual(webserver._web_bad_ips, {})

    def test_the_operator_logs_in_after_it_tried_three_times(self):
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.rebound_post()

        resp = self.client.post("/login", data={"password": PASSWORD},
                                headers={"Host": "127.0.0.1:8420",
                                         "Origin": "http://127.0.0.1:8420"})

        self.assertEqual(resp.status_code, 302)

    def test_the_right_password_under_the_rebound_name_does_not_log_in(self):
        resp = self.rebound_post(password=PASSWORD)

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("Set-Cookie", resp.headers)

    def test_the_api_is_refused_under_the_rebound_name_even_when_logged_in(self):
        self.log_in()

        resp = self.client.get("/api/settings", headers={"Host": REBOUND})

        self.assertEqual(resp.status_code, 403)
        self.assertIn("WEBUI_ALLOWED_HOSTS", resp.get_json()["error"])

    def test_a_page_is_refused_with_a_plain_explanation(self):
        resp = self.client.get("/login", headers={"Host": REBOUND})

        self.assertEqual(resp.status_code, 403)
        self.assertIn(b"rebound-site.example", resp.data)

    def test_a_lan_user_who_opens_it_by_ip_still_logs_in(self):
        """WEBUI_HOST = 0.0.0.0 and http://192.168.1.20:8420/ - the common
        LAN setup must keep working."""
        self.set_config(WEBUI_HOST="0.0.0.0")
        client = self.app.test_client()

        resp = client.post("/login", data={"password": PASSWORD},
                           base_url="http://192.168.1.20:8420",
                           headers={"Origin": "http://192.168.1.20:8420"})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(client.get("/api/queue", base_url="http://192.168.1.20:8420").status_code, 200)

    def test_a_configured_name_logs_in(self):
        self.set_config(WEBUI_ALLOWED_HOSTS=["rebound-site.example"])

        self.assertEqual(self.rebound_post(password=PASSWORD).status_code, 302)


if __name__ == "__main__":
    unittest.main()
