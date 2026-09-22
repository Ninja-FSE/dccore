"""Every dashboard route is behind the login, and the four that had no HTTP
test now have one.

WHY THIS FILE EXISTS

The dashboard has 37 registered rules and not one @login_required decorator.
Everything rests on a single before_request hook that denies by default and
exempts exactly one endpoint. That design is right - a per-route decorator is
a thing somebody can forget to add, and a global hook is not - but it puts the
whole authentication story on one function, and nothing tested it as a whole.

An audit probed all 37 rules unauthenticated and found none reachable, so the
gate holds today. What was missing is anything that would notice if it stopped
holding. Three realistic ways it could:

  * the exemption widens - a second endpoint added to the `== "login"` check,
    or a well-meant "let the static files through" clause
  * the check becomes path-based again, at which point any route sharing a
    prefix with /login rides in with it
  * a future blueprint or second Flask app registers routes that this app's
    before_request never sees - the setup app IS that second app, and gets
    its own walk below (#674)

The test below walks url_map itself, so a route added tomorrow is covered
without anybody remembering this file exists. That is the property worth
having: not "these 37 are gated" but "whatever is registered is gated".

THE FOUR ROUTES WITH NO HTTP TEST

docs/FUTURE.md carried "nine dashboard routes were never requested by any
test; that count has not been re-measured since". Re-measured: four.

Their BUILDERS are well covered - build_crosslist_search_payload,
apply_on_connect_changes, build_stats_import_preview and the rest have six to
twelve tests each. What no test touched is the wiring in front of them: that
the path resolves, that the method restriction is real, and that the JSON
envelope comes back. A route pointed at the wrong builder, or accidentally
registered GET-and-POST, would have passed everything.
"""

import inspect
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

PASSWORD = "route-audit-password"

# The one endpoint the gate is allowed to exempt. Named by ENDPOINT, the same
# way the hook matches it - checking the path here would pass against exactly
# the path-prefix regression this file is meant to catch.
PUBLIC_ENDPOINTS = {"login"}


class RouteCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.set_config(
            ADMIN_PASSWORD_HASH=adminchat.make_password_hash(PASSWORD, iterations=1000),
            LOCAL_LIST_DIR=self.tree.lists, FILE_DIRECTORY=self.tree.music,
            LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
            WEBUI_CONSOLE_ENABLED=True)
        self.app = webserver.create_app()
        self.client = self.app.test_client()
        webserver._web_bad_ips.clear()

    def log_in(self):
        resp = self.client.post("/login", data={"password": PASSWORD})
        self.assertEqual(resp.status_code, 302, "the fixture's own login failed")

    def every_rule(self):
        """(endpoint, concrete path, method) for everything registered.

        Variable parts are filled with a value that will not resolve to
        anything real - the question is whether the gate answers before the
        handler does, and a 404 from inside a handler would mean it did not.
        """
        for rule in self.app.url_map.iter_rules():
            path = str(rule)
            for name in rule.arguments:
                path = path.replace(f"<{name}>", "no-such-thing")
                for converter in ("path:", "int:", "string:"):
                    path = path.replace(f"<{converter}{name}>", "no-such-thing")
            for method in sorted(rule.methods & {"GET", "POST", "PUT", "DELETE"}):
                yield rule.endpoint, path, method


class NothingIsReachableWithoutLoggingIn(RouteCase):

    def test_every_registered_rule_refuses_an_anonymous_request(self):
        """Walks url_map rather than a hand-written list, so a route added
        later is covered without anybody remembering this test exists."""
        for endpoint, path, method in self.every_rule():
            if endpoint in PUBLIC_ENDPOINTS:
                continue
            with self.subTest(endpoint=endpoint, path=path, method=method):
                resp = self.client.open(path, method=method)

                self.assertIn(
                    resp.status_code, (301, 302, 303, 307, 308, 401),
                    f"{method} {path} answered {resp.status_code} to an "
                    f"anonymous caller instead of refusing it")

    def test_the_api_refuses_with_401_rather_than_a_redirect(self):
        """A browser redirect handed to fetch() looks like success with a
        login page for a body, and the dashboard would render it as data."""
        for endpoint, path, method in self.every_rule():
            if endpoint in PUBLIC_ENDPOINTS or not path.startswith("/api/"):
                continue
            with self.subTest(path=path, method=method):
                resp = self.client.open(path, method=method)

                self.assertEqual(resp.status_code, 401)

    def test_only_the_login_endpoint_is_exempt(self):
        """Guard on the allow-list itself: if a future exemption is added to
        the hook, this names it rather than quietly widening."""
        reachable = set()
        for endpoint, path, method in self.every_rule():
            resp = self.client.open(path, method=method)
            if resp.status_code not in (301, 302, 303, 307, 308, 401):
                reachable.add(endpoint)

        self.assertEqual(reachable, PUBLIC_ENDPOINTS)

    def test_the_exemption_is_not_matched_by_path_prefix(self):
        """The hook matches request.endpoint, not request.path, and says why:
        a path test would let any future route sharing the prefix through.
        There is no such route today, so this asks the question directly."""
        hook = inspect.getsource(webserver.create_app)
        self.assertIn('request.endpoint == "login"', hook)
        self.assertNotIn('request.path.startswith("/login")', hook)

    def test_logging_in_actually_opens_it(self):
        """Control. Every assertion above would also pass if the dashboard
        refused everybody, including the operator."""
        self.log_in()

        self.assertEqual(self.client.get("/api/queue").status_code, 200)


class TheFourRoutesThatHadNoHttpTest(RouteCase):
    """Their builders have six to twelve tests each. The wiring in front of
    them had none."""

    def test_the_cross_list_search_route_answers_json(self):
        self.log_in()

        resp = self.client.get("/api/filelists/search?q=nothing-matches-this")

        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(json.loads(resp.data), dict)

    def test_the_lists_route_answers_json(self):
        self.log_in()

        resp = self.client.get("/api/lists")

        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(json.loads(resp.data), dict)

    def test_the_on_connect_route_answers_json(self):
        self.log_in()

        resp = self.client.get("/api/on-connect")

        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(json.loads(resp.data), dict)

    def test_a_get_does_not_reach_the_preview_handler(self):
        """It carries a vars.ini file's contents in the body. A GET-able
        version would put that in a URL, and in every log along the way.

        Asserted as "refused", not as 405: this app answers a wrong-method
        request with 404, which test_dashboard_routes.py already ran into on
        /api/tools/update-list. The status alone is therefore ambiguous - 404
        is also what a route that does not exist returns - so the rule itself
        is checked separately below."""
        self.log_in()

        self.assertGreaterEqual(
            self.client.get("/api/stats/import/preview").status_code, 400)

    def test_the_preview_rule_is_registered_post_only(self):
        """The other half: without this, the assertion above would pass just
        as happily if the route had been deleted."""
        methods = set()
        for rule in self.app.url_map.iter_rules():
            if str(rule) == "/api/stats/import/preview":
                methods = set(rule.methods)

        self.assertIn("POST", methods)
        self.assertNotIn("GET", methods)

    def test_the_stats_import_preview_route_answers_json(self):
        self.log_in()

        resp = self.client.post("/api/stats/import/preview",
                                json={"text": ""})

        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(json.loads(resp.data), dict)


if __name__ == "__main__":
    unittest.main()


class EverySetupRouteIsBehindTheToken(DCCoreTestCase):
    """The second Flask app the docstring above warns about (#674, audit
    L10): create_setup_app() has its own before_request gate and its own
    url_map, and nothing walked it. Each of its three routes was pinned by
    hand in test_set_it_up_in_the_browser.py, so this is a guard against a
    route or an exemption added tomorrow, not a hole today. Walked the
    same way: every rule, every method, and the gate has to answer - with
    a foreign Host, without the token, and with the token but a foreign
    Host."""

    def setUp(self):
        super().setUp()
        self.done = []
        self.app = webserver.create_setup_app("the-token", self.done.append, port=8420)
        self.client = self.app.test_client()

    def every_rule(self):
        for rule in self.app.url_map.iter_rules():
            path = str(rule)
            for name in rule.arguments:
                path = path.replace(f"<{name}>", "no-such-thing")
                for converter in ("path:", "int:", "string:"):
                    path = path.replace(f"<{converter}{name}>", "no-such-thing")
            for method in sorted(rule.methods & {"GET", "POST", "PUT", "DELETE"}):
                yield rule.endpoint, path, method

    def test_the_walk_sees_the_routes_it_is_meant_to(self):
        """A walk of nothing would pass the tests below."""
        self.assertEqual(sorted({path for _e, path, _m in self.every_rule()}),
                         ["/", "/login", "/setup"])

    def test_every_rule_refuses_a_request_without_the_token(self):
        for endpoint, path, method in self.every_rule():
            with self.subTest(endpoint=endpoint, path=path, method=method):
                resp = self.client.open(path, method=method, headers={"Host": "127.0.0.1:8420"})

                self.assertEqual(resp.status_code, 403,
                                 f"{method} {path} answered {resp.status_code} without the token")
                self.assertIn(b"one-time code", resp.data)

    def test_every_rule_refuses_a_foreign_host_even_with_the_token(self):
        for endpoint, path, method in self.every_rule():
            with self.subTest(endpoint=endpoint, path=path, method=method):
                resp = self.client.open(path + "?token=the-token", method=method,
                                        headers={"Host": "evil.example:8420"})

                self.assertEqual(resp.status_code, 403,
                                 f"{method} {path} answered {resp.status_code} to a foreign host")
                self.assertIn(b"127.0.0.1", resp.data)

    def test_nothing_was_applied_by_any_of_it(self):
        for _endpoint, path, method in self.every_rule():
            self.client.open(path, method=method, headers={"Host": "127.0.0.1:8420"})
            self.client.open(path + "?token=the-token", method=method, headers={"Host": "evil.example"})

        self.assertEqual(self.done, [])
