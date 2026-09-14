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

import adminchat
import announce  # noqa: E402
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
    ("/api/console/log", "build_console_log_payload"),
    ("/api/notices", "build_notices_payload"),
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
            LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
            # The Console ships OFF (WEBUI_CONSOLE_ENABLED) - it puts the admin
            # command set behind one factor instead of two, so it is opted into
            # rather than granted by turning the dashboard on. Enabled here
            # because these tests are about the routes' wiring; the gate itself
            # is TheConsoleGate below.
            WEBUI_CONSOLE_ENABLED=True)
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


class TheConsoleCommandRoute(DashboardRouteCase):
    """/api/console/command is the one mutating route in this file with no
    query string to pass through - the body is JSON, not ?q=. See
    tests/test_web_console.py for build_console_command_result() itself
    (command dispatch, 'quit', unrecognised names); this is only the wiring:
    does the route reach that builder with what the caller actually sent."""

    def test_it_passes_the_command_through(self):
        self.log_in()

        resp = self.client.post("/api/console/command", json={"command": "uptime"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(),
                         webserver.build_console_command_result("uptime")[1])

    def test_a_missing_body_is_treated_as_an_empty_command(self):
        """Not a 500. json_object() is what every other mutating route in
        this file already falls back to for a malformed or absent body."""
        self.log_in()

        resp = self.client.post("/api/console/command")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_the_command_actually_reaches_the_builder(self):
        """Control for the pass-through test above: if the route ignored the
        body entirely, that test would still pass, because it compares
        against a builder call that used the same hardcoded string."""
        self.log_in()
        recorded = []
        real = webserver.build_console_command_result
        webserver.build_console_command_result = (
            lambda command, remote_addr=None: recorded.append(command) or real(command))
        self.addCleanup(setattr, webserver, "build_console_command_result", real)

        self.client.post("/api/console/command", json={"command": "status"})

        self.assertEqual(recorded, ["status"])

    def test_it_refuses_an_unauthenticated_caller(self):
        resp = self.client.post("/api/console/command", json={"command": "uptime"})

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


class ThePurgeRoute(DashboardRouteCase):
    """The wiring only - what a purge actually removes is in
    tests/test_purging_a_fetched_list.py."""

    def test_the_source_reaches_the_builder(self):
        self.log_in()

        resp = self.client.post("/api/filelists/NoSuchBot/purge")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(),
                         webserver.build_fetched_list_purge_result(
                             "NoSuchBot")[1])

    def test_a_source_with_a_slash_in_it_reaches_the_route_at_all(self):
        """A bot's other lists are named "<nick>/<marker>". A plain <source>
        converter stops at the slash, so those rows - the ones most likely to
        be purged, since they are the duplicates - would have 404'd on the
        ROUTE rather than reaching the builder. <path:source> is what makes
        them addressable."""
        self.log_in()

        resp = self.client.post("/api/filelists/NoSuchBot/films/purge")

        self.assertEqual(resp.status_code, 404)
        self.assertIn("NoSuchBot", resp.get_json().get("error", ""))

    def test_a_get_does_not_purge(self):
        """POST-only: it deletes files, and a GET that mutates gets fired by
        anything that prefetches links.

        Asserted on the STORE rather than on a status code. Whether the
        framework answers 405 or 404 for a wrong method is its business and
        has changed between versions; what must be true is that nothing was
        deleted.
        """
        self.log_in()
        config.fetched_bot_lists["somebot"] = {"bot": "SomeBot",
                                               "entry_count": 1, "lists": {}}

        self.client.get("/api/filelists/SomeBot/purge")

        self.assertIn("somebot", config.fetched_bot_lists)


class TheNoticesReadRoute(DashboardRouteCase):
    """/api/notices/read, the wiring only. What the builder decides - which
    notices are acknowledged, what the badge says afterwards - is in
    tests/test_tell_me_what_i_missed.py.

    POST rather than GET because it CHANGES what the operator has
    acknowledged, and a GET that mutates gets fired by anything that prefetches
    links.
    """

    def test_it_answers_with_the_payload_the_builder_produces(self):
        self.log_in()
        announce.record_notice("Kicked from #example.", "warning")

        resp = self.client.post("/api/notices/read")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["unread"], 0)
        self.assertEqual(len(resp.get_json()["notices"]), 1)

    def test_it_actually_marks_them_read(self):
        """Control for the test above: a route that answered with a fresh
        payload without marking anything would satisfy an equality check
        against a builder call made after it."""
        self.log_in()
        announce.record_notice("Kicked from #example.", "warning")

        self.client.post("/api/notices/read")

        self.assertEqual(announce.unread_notices(), (0, ""))

    def test_a_get_does_not_mark_them_read(self):
        """The read route is POST-only, and the listing route beside it must
        not quietly acknowledge things merely by being looked at - the panel
        polls it every couple of seconds whether anybody is on that page."""
        self.log_in()
        announce.record_notice("Kicked from #example.", "warning")

        self.client.get("/api/notices")

        self.assertEqual(announce.unread_notices(), (1, "warning"))


class TheConsoleGate(DashboardRouteCase):
    """WEBUI_CONSOLE_ENABLED, and why it exists.

    The two ways to reach the admin command set are not equally protected: the
    DCC CHAT console needs the operator's services host AND the password, the
    dashboard needs the password alone, over HTTP with no TLS. So the Console
    page puts ban/unban/clearqueue/rehash/update behind the weaker door.

    That is a fine trade for an operator who wants it. Without this switch it
    would not have been a trade at all - anyone who had turned the dashboard on
    for Search and Queue would have gained a remote admin console on upgrade,
    no setting changed, nothing recording that their exposure had widened.
    """

    def setUp(self):
        super().setUp()
        self.set_config(WEBUI_CONSOLE_ENABLED=False)
        self.app = webserver.create_app()
        self.client = self.app.test_client()

    def test_the_log_route_is_not_there_when_the_console_is_off(self):
        self.log_in()

        self.assertEqual(self.client.get("/api/console/log").status_code, 404)

    def test_the_command_route_is_not_there_when_the_console_is_off(self):
        """Checked separately from the log route on purpose. They are the whole
        attack surface of this feature, and a gate applied to one and forgotten
        on the other is a silent hole - which is exactly the shape of mistake
        that would leave the mutating route open while the harmless one was
        shut."""
        self.log_in()

        resp = self.client.post("/api/console/command", json={"command": "uptime"})

        self.assertEqual(resp.status_code, 404)

    def test_it_is_404_rather_than_403(self):
        """403 would confirm the routes exist and are merely switched off,
        which tells anyone probing that this build has an admin console worth
        coming back for."""
        self.log_in()
        resp = self.client.get("/api/console/log")

        self.assertEqual(resp.status_code, 404)
        self.assertNotEqual(resp.status_code, 403)

    def test_still_closed_to_someone_who_never_logged_in(self):
        """Control on the pair above: the gate must not have replaced the login
        with itself. An unauthenticated request is turned away whether the
        console is on or off."""
        self.assertIn(self.client.get("/api/console/log").status_code, (302, 401, 404))

    def test_the_rest_of_the_dashboard_is_unaffected(self):
        """Control. A gate that took the whole dashboard down with it would
        satisfy every assertion above."""
        self.log_in()

        self.assertEqual(self.client.get("/api/stats").status_code, 200)
        self.assertEqual(self.client.get("/api/queue").status_code, 200)


class WhatNeedsARestartToTakeEffect(DashboardRouteCase):
    """#302: "when a user changes ports or other core configurations that
    affect irc.py and dcc.py, the changes do not take effect immediately."

    The notice already existed - the save returns `restart_required` and the
    page shows it. SERVER and PORT were simply not in the set, so the two
    settings the issue actually names were saved in silence.
    """

    def test_the_server_and_port_are_named(self):
        self.assertIn("SERVER", webserver.SETTINGS_RESTART_ONLY)
        self.assertIn("PORT", webserver.SETTINGS_RESTART_ONLY)

    def test_settings_a_rehash_really_does_apply_are_not(self):
        """Listing those would train an operator to ignore the notice. The DCC
        port range is read per send, so a rehash applies it to the very next
        transfer; the channel list is JOIN/PARTed by the rehash itself."""
        for name in ("DCC_PORT_START", "DCC_PORT_END", "CHANNEL",
                     "FILE_DIRECTORY", "MAX_DCC_SLOTS"):
            with self.subTest(name=name):
                self.assertNotIn(name, webserver.SETTINGS_RESTART_ONLY)

    def test_there_is_one_answer_to_the_question(self):
        """A second set was written and deleted during this change. Two
        answers to "does this need a restart" is how they drift."""
        self.assertFalse(hasattr(webserver, "SETTINGS_NEEDING_RESTART"))

    def test_a_save_that_changes_the_server_says_so(self):
        self.log_in()

        resp = self.client.post("/api/settings", json={"SERVER": "irc.example.org"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn("SERVER", resp.get_json().get("restart_required", []))

    def test_a_save_that_changes_something_live_does_not(self):
        """The notice is only worth anything if it stays rare."""
        self.log_in()

        resp = self.client.post("/api/settings", json={"MAX_DCC_SLOTS": "4"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json().get("restart_required", []), [])


class WhenTheConsoleIsOnByDefault(DCCoreTestCase):
    """Asked for as "on by default on windows machines". Keyed on EXPOSURE
    instead, which gives the same answer on a normal Windows desktop and does
    not create the case the setting exists to prevent.

    The Console puts ban, unban, clearqueue, rehash and update behind the
    dashboard password alone - one factor, over plain HTTP. That is a real
    widening when the dashboard is on the LAN, and no widening at all when it
    is on loopback, where whoever can reach it already has the served files
    and admin_config.py.
    """

    def test_loopback_means_on(self):
        self.set_config(WEBUI_HOST="127.0.0.1", WEBUI_CONSOLE_ENABLED=None)

        self.assertTrue(webserver.console_is_enabled())

    def test_localhost_counts_as_loopback(self):
        self.set_config(WEBUI_HOST="localhost", WEBUI_CONSOLE_ENABLED=None)

        self.assertTrue(webserver.console_is_enabled())

    def test_a_lan_address_means_off(self):
        """The paragraph in defaults.py applies in full here: turning the
        dashboard on for Search and Queue must not grant remote admin as a
        side effect."""
        for host in ("0.0.0.0", "192.168.1.20", "10.0.0.5"):
            with self.subTest(host=host):
                self.set_config(WEBUI_HOST=host, WEBUI_CONSOLE_ENABLED=None)
                self.assertFalse(webserver.console_is_enabled())

    def test_a_host_it_cannot_read_is_treated_as_exposed(self):
        """A host this cannot parse is a host it cannot vouch for, and the
        safe answer to "is this exposed" is yes."""
        self.set_config(WEBUI_HOST="some-hostname.local",
                        WEBUI_CONSOLE_ENABLED=None)

        self.assertFalse(webserver.console_is_enabled())

    def test_an_explicit_choice_always_wins(self):
        """Nobody who has already made this choice has it made again for them
        on upgrade - which is the whole objection to changing a default."""
        self.set_config(WEBUI_HOST="127.0.0.1", WEBUI_CONSOLE_ENABLED=False)
        self.assertFalse(webserver.console_is_enabled())

        self.set_config(WEBUI_HOST="0.0.0.0", WEBUI_CONSOLE_ENABLED=True)
        self.assertTrue(webserver.console_is_enabled())

    def test_the_routes_ask_the_same_question(self):
        """Reading the setting directly in the route would keep the old flat
        default alive on the only paths that actually gate the feature."""
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as handle:
            code = handle.read()
        gate = code.split("def _console_is_available():", 1)[1][:400]

        self.assertIn("console_is_enabled()", gate)
        self.assertNotIn('getattr(config, "WEBUI_CONSOLE_ENABLED"', gate)


class OpeningTheDashboardOnStartup(DCCoreTestCase):
    """"when it is enabled i think the website should auto open on the default
    browser when you start up the program"."""

    def test_it_opens_for_a_loopback_dashboard(self):
        self.set_config(WEBUI_HOST="127.0.0.1", WEBUI_OPEN_BROWSER=True)
        opened = []

        self.assertTrue(webserver._open_in_browser("127.0.0.1", 8420,
                                                   opener=opened.append))
        self.assertEqual(opened, ["http://127.0.0.1:8420/"])

    def test_it_does_not_open_on_a_lan_bound_dashboard(self):
        """Not about security - about what the machine probably is. A
        dashboard on the LAN is as likely to be a headless box as a desktop,
        and a daemon spawning a browser there is doing something nobody asked
        for and nobody will see."""
        self.set_config(WEBUI_HOST="0.0.0.0", WEBUI_OPEN_BROWSER=True)
        opened = []

        self.assertFalse(webserver._open_in_browser("0.0.0.0", 8420,
                                                    opener=opened.append))
        self.assertEqual(opened, [])

    def test_it_can_be_turned_off(self):
        self.set_config(WEBUI_HOST="127.0.0.1", WEBUI_OPEN_BROWSER=False)
        opened = []

        self.assertFalse(webserver._open_in_browser("127.0.0.1", 8420,
                                                    opener=opened.append))
        self.assertEqual(opened, [])

    def test_a_machine_with_no_browser_still_starts_the_bot(self):
        """A missing browser, no display, or a wayward BROWSER variable is not
        a reason to stop the daemon - the address was printed a line above."""
        self.set_config(WEBUI_HOST="127.0.0.1", WEBUI_OPEN_BROWSER=True)
        said = []

        def explode(_url):
            raise RuntimeError("no display")

        result = webserver._open_in_browser("127.0.0.1", 8420, opener=explode,
                                            log=said.append)

        self.assertFalse(result)
        self.assertIn("still running at", chr(10).join(said))

    def test_startup_calls_it(self):
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as handle:
            code = handle.read()

        self.assertIn("_open_in_browser(host, port)", code)


if __name__ == "__main__":
    unittest.main()
