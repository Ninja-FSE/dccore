"""Every dashboard route is requested at least once, by a real HTTP request.

WHAT WAS MISSING

The suite tests webserver.py's payload BUILDERS thoroughly - build_stats_payload,
build_search_payload, build_fetch_status_payload and the rest all have their own
cases. What nothing tested was the WIRING: the two lines of each route that
decide which builder runs, what arguments reach it, and whether the login gate
sits in front.

Profiling the suite showed nine of the twenty-three route handlers were never
entered by any test, including `/` itself and all three `/api/tools/*` - the
three that mutate state. A route wired to the wrong builder, or one that quietly
lost its login gate, would have passed every test in the file.

    /                                    index()
    /api/search                          api_search()
    /api/stats                           api_stats()
    /api/fetch/status                    api_fetch_status()
    /api/fetch/<request_id>/download     api_fetch_download()
    /api/search/broadcast/status         api_search_broadcast_status()
    /api/tools/update-list               api_tools_update_list()
    /api/tools/update-list/status        api_tools_update_list_status()
    /api/tools/verify-list               api_tools_verify_list()

HOW THEY ARE TESTED

Against the builder's own return value, not against a hardcoded shape: asserting
that `GET /api/stats` returns exactly what `build_stats_payload()` returns proves
the route calls the right function, and leaves what that function should CONTAIN
to the builder's own tests, where it already is. A route rewired to a different
builder fails here; a change to a payload's contents does not.

The state-mutating route is driven with its worker replaced, because
`/api/tools/update-list` starts a real subprocess that walks the music library.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

PASSWORD = "test-password"

# Every route below is a GET that should answer 200 to a logged-in client, and
# the builder its payload must match.
READ_ONLY_ROUTES = [
    ("/api/stats", "build_stats_payload"),
    ("/api/fetch/status", "build_fetch_status_payload"),
    ("/api/search/broadcast/status", "build_broadcast_status_payload"),
    ("/api/tools/update-list/status", "build_update_list_status_payload"),
    ("/api/tools/verify-list", "build_verify_list_payload"),
]


@unittest.skipUnless(webserver.HAVE_FLASK,
                     "Flask not installed; CI installs requirements-web.txt so "
                     "these run there")
class DashboardRouteCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.set_config(
            ADMIN_PASSWORD_HASH=adminchat.make_password_hash(PASSWORD, iterations=1000),
            LOCAL_LIST_DIR=self.tree.lists, FILE_DIRECTORY=self.tree.music,
            LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest")
        self.app = webserver.create_app()
        self.client = self.app.test_client()
        webserver._web_bad_ips.clear()

    def log_in(self):
        resp = self.client.post("/login", data={"password": PASSWORD})
        self.assertEqual(resp.status_code, 302, "the fixture's own login failed")


class TheReadOnlyRoutesAnswer(DashboardRouteCase):

    def test_each_one_returns_the_payload_its_builder_produces(self):
        """The wiring, not the contents. A route pointed at the wrong builder
        is exactly the defect nothing could previously catch."""
        self.log_in()
        for path, builder in READ_ONLY_ROUTES:
            with self.subTest(route=path):
                resp = self.client.get(path)

                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.get_json(),
                                 getattr(webserver, builder)())

    def test_each_one_refuses_an_unauthenticated_caller(self):
        """An API caller gets a JSON 401, not a redirect to an HTML page."""
        for path, _builder in READ_ONLY_ROUTES:
            with self.subTest(route=path):
                resp = self.client.get(path)

                self.assertEqual(resp.status_code, 401)
                self.assertIn("error", resp.get_json())


class TheSearchRoute(DashboardRouteCase):
    """/api/search takes a parameter, so it can be wired up and still be wrong."""

    def test_it_passes_the_query_through(self):
        self.log_in()

        resp = self.client.get("/api/search?q=sandman")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), webserver.build_search_payload("sandman"))

    def test_a_missing_query_is_treated_as_an_empty_one(self):
        """Not a 500. Someone hitting the endpoint by hand should get the same
        answer as an empty search box."""
        self.log_in()

        resp = self.client.get("/api/search")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), webserver.build_search_payload(""))

    def test_the_query_actually_reaches_the_builder(self):
        """Control for the two above: if the route ignored q entirely, both
        would still pass, because both compare against a builder call."""
        self.log_in()
        recorded = []
        real = webserver.build_search_payload
        webserver.build_search_payload = lambda q: recorded.append(q) or real(q)
        self.addCleanup(setattr, webserver, "build_search_payload", real)

        self.client.get("/api/search?q=metallica")

        self.assertEqual(recorded, ["metallica"])

    def test_it_refuses_an_unauthenticated_caller(self):
        resp = self.client.get("/api/search?q=x")

        self.assertEqual(resp.status_code, 401)


class TheIndexRoute(DashboardRouteCase):
    """`/` serves the dashboard itself. Never requested by any test until now,
    which is a strange gap for the page every operator opens first."""

    def test_a_logged_in_visitor_gets_the_page(self):
        self.log_in()

        resp = self.client.get("/")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("html", resp.headers.get("Content-Type", "").lower())

    def test_a_visitor_who_is_not_logged_in_is_sent_to_the_login_form(self):
        """A browser navigation, so a redirect is right here where a JSON 401
        is right for the API routes."""
        resp = self.client.get("/")

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/login"))


class TheListUpdateRoute(DashboardRouteCase):
    """/api/tools/update-list mutates state: it starts a subprocess that walks
    the whole music library. Driven with the worker replaced."""

    def setUp(self):
        super().setUp()
        self.started = []
        real = webserver.start_list_update
        webserver.start_list_update = lambda: (
            self.started.append(True), (200, {"started": True}))[1]
        self.addCleanup(setattr, webserver, "start_list_update", real)

    def test_a_post_starts_the_update(self):
        self.log_in()

        resp = self.client.post("/api/tools/update-list")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.started, [True])

    def test_a_get_does_not(self):
        """It is POST-only for a reason: a state-changing endpoint reachable by
        GET is one a browser prefetch, a link, or an <img src> can fire.

        Asserted as "an error, and nothing happened" rather than as 405. The
        rule really is POST-only - url_map reports methods={OPTIONS, POST} -
        but this app answers a GET to it with 404, not the 405 the method
        mismatch would suggest. Pinning 405 would be pinning an assumption
        about Flask rather than the property that matters."""
        self.log_in()

        resp = self.client.get("/api/tools/update-list")

        self.assertGreaterEqual(resp.status_code, 400)
        self.assertEqual(self.started, [])

    def test_the_rule_is_registered_post_only(self):
        """The other half of the test above: 404 could equally mean the route
        does not exist, which would make that assertion pass for the wrong
        reason."""
        methods = set()
        for rule in self.app.url_map.iter_rules():
            if str(rule) == "/api/tools/update-list":
                methods = set(rule.methods)

        self.assertIn("POST", methods)
        self.assertNotIn("GET", methods)

    def test_an_unauthenticated_post_does_not_start_anything(self):
        """The one that matters. This route runs a subprocess on the host."""
        resp = self.client.post("/api/tools/update-list")

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.started, [])


class TheFetchDownloadRoute(DashboardRouteCase):
    """/api/fetch/<request_id>/download hands a file that came from another
    bot to the browser."""

    def test_an_unknown_request_is_not_found(self):
        self.log_in()

        resp = self.client.get("/api/fetch/no-such-request/download")

        self.assertEqual(resp.status_code, 404)

    def test_an_incomplete_fetch_is_not_served(self):
        """A partial file must not be handed over as though it were the whole
        thing."""
        self.log_in()
        config.fetch_queue["pending-one"] = {
            "request_type": "file", "complete": False, "stored_filename": None}
        self.addCleanup(config.fetch_queue.pop, "pending-one", None)

        resp = self.client.get("/api/fetch/pending-one/download")

        self.assertEqual(resp.status_code, 404)

    def test_it_refuses_an_unauthenticated_caller(self):
        resp = self.client.get("/api/fetch/anything/download")

        self.assertEqual(resp.status_code, 401)


class TheQueueViewIgnoresAMalformedTransfer(DashboardRouteCase):
    """#236 made the Queue summary take senders from active_transfers as well
    as dcc_queue, which fixed a real blind spot - a user sending with a free
    slot and nothing queued never enters dcc_queue at all.

    It also changed where the row keys come from. dcc_queue's keys are always
    real nicks; active_transfers holds dicts that dcc.py builds, and an entry
    missing its "user" field contributes "" to the sender set. That reached the
    page as a blank row with a "?" preview - not reachable through dcc.py's own
    code today, but it was not reachable at all before the change.
    """

    def test_an_entry_with_no_user_produces_no_row(self):
        config.dcc_queue.clear()
        config.active_transfers[:] = []
        self.addCleanup(config.active_transfers.clear)
        config.active_transfers.append({"bytes_sent": 0})

        rows = webserver.build_queue_payload()

        self.assertEqual(rows, [], "a malformed transfer reached the page")

    def test_a_well_formed_entry_still_appears(self):
        """Control: filtering must not swallow the fix #236 made."""
        config.dcc_queue.clear()
        config.active_transfers[:] = []
        self.addCleanup(config.active_transfers.clear)
        config.active_transfers.append({"user": "Dave", "file": "Song.flac"})

        rows = webserver.build_queue_payload()

        self.assertEqual([r["user"] for r in rows], ["dave"])
        self.assertEqual(rows[0]["status"], "sending")
        self.assertEqual(rows[0]["preview"], "Song.flac")


if __name__ == "__main__":
    unittest.main()
