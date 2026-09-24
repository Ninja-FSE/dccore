"""#572: the bot says when a newer DCCore has been released.

Once a day it asks GitHub for the latest release of the repository PROJECT_URL
names, and says what it found on the dashboard, in the console's `status` and
in the debug feed (the mIRC window). On by default and said at startup; a check
that fails says why - "it should write somewhere that it didnt manage to
communicate with github" - and a manual check works with the daily one off.

No test here reaches the network: every check is handed a fake GitHub, either
as `fetch=` or as an `opener` standing in for urllib.request.urlopen.
"""

import contextlib
import io
import json
import os
import socket
import sys
import time
import unittest
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import runtime  # noqa: E402
import version_check  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402
from tests.test_admin_console_commands import FakeSession  # noqa: E402


def release(tag="v1.14.0", prerelease=False, url=None):
    """What fetch_latest() returns for a release."""
    return lambda: {"tag": tag, "url": url or f"https://github.com/example/dccore/releases/tag/{tag}",
                    "prerelease": prerelease}


def unreachable(reason="could not connect to GitHub (timed out)"):
    def fetch():
        raise version_check.CheckFailed(reason)
    return fetch


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self, limit=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class VersionCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(SCRIPT_VERSION="DCCore v1.13.0", PROJECT_URL="https://github.com/example/dccore",
                        CHECK_FOR_UPDATES=True)
        self.feed = silence_debug(announce)

    def said(self):
        return "\n".join(text for _category, text in self.feed)


class ReadingVersions(VersionCase):
    def test_a_release_candidate_is_behind_the_release_of_its_number(self):
        self.assertLess(version_check.running_version("DCCore v1.14.0-RC1"),
                        version_check.release_version("v1.14.0"))
        self.assertEqual(version_check.running_version("DCCore v1.13.0"),
                         version_check.release_version("v1.13.0"))

    def test_only_a_plain_release_tag_is_compared(self):
        for tag in ("v1.14.0-RC1", "v1.14", "latest", "", None):
            self.assertIsNone(version_check.release_version(tag), tag)
        self.assertEqual(version_check.release_version("1.14.2"), (1, 14, 2, 1))

    def test_numbers_compare_as_numbers(self):
        self.assertGreater(version_check.release_version("v1.10.0"), version_check.running_version("DCCore v1.9.9"))

    def test_the_repository_comes_from_project_url(self):
        self.assertEqual(version_check.repository(), "example/dccore")
        self.assertEqual(version_check.repository("https://github.com/example/dccore.git"), "example/dccore")
        self.assertEqual(version_check.repository("https://github.com/example/dccore/"), "example/dccore")
        self.assertIsNone(version_check.repository("https://example.org/dccore"))


class AskingGitHub(VersionCase):
    def opener_returning(self, payload):
        self.requests = []

        def opener(request, timeout=None):
            self.requests.append((request, timeout))
            return FakeResponse(json.dumps(payload).encode("utf-8"))
        return opener

    def opener_raising(self, error):
        def opener(request, timeout=None):
            raise error
        return opener

    def test_one_get_for_the_latest_release_carrying_nothing(self):
        opener = self.opener_returning({"tag_name": "v1.14.0", "html_url": "https://github.com/example/dccore/releases/tag/v1.14.0"})
        latest = version_check.fetch_latest(opener)
        self.assertEqual(latest, {"tag": "v1.14.0", "prerelease": False,
                                  "url": "https://github.com/example/dccore/releases/tag/v1.14.0"})
        (request, timeout), = self.requests
        self.assertEqual(request.full_url, "https://api.github.com/repos/example/dccore/releases/latest")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data, "nothing about the bot is sent")
        self.assertEqual(timeout, version_check.REQUEST_TIMEOUT_SECONDS)

    def test_every_failure_becomes_a_reason_the_operator_can_read(self):
        limited = urllib.error.HTTPError("u", 403, "Forbidden", {"X-RateLimit-Remaining": "0"}, None)
        cases = [
            (limited, "hourly limit"),
            (urllib.error.HTTPError("u", 404, "Not Found", {}, None), "no published release for example/dccore"),
            (urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None), "HTTP 502"),
            (urllib.error.URLError("getaddrinfo failed"), "could not connect to GitHub (getaddrinfo failed)"),
            (socket.timeout("timed out"), "could not connect to GitHub (timed out)"),
        ]
        for error, words in cases:
            if isinstance(error, urllib.error.HTTPError):
                self.addCleanup(error.close)
            with self.assertRaises(version_check.CheckFailed) as caught:
                version_check.fetch_latest(self.opener_raising(error))
            self.assertIn(words, str(caught.exception))

    def test_an_answer_that_is_not_a_release_is_a_failure(self):
        for body in (b"<html>", b"[]", b'{"message": "x"}'):
            opener = lambda request, timeout=None, body=body: FakeResponse(body)
            with self.assertRaises(version_check.CheckFailed):
                version_check.fetch_latest(opener)

    def test_no_request_without_a_github_repository(self):
        self.set_config(PROJECT_URL="https://example.org/dccore")
        opener = self.opener_returning({})
        with self.assertRaises(version_check.CheckFailed) as caught:
            version_check.fetch_latest(opener)
        self.assertIn("PROJECT_URL", str(caught.exception))
        self.assertEqual(self.requests, [])


class AFailureIsNeverSilent(VersionCase):
    def test_the_daily_check_says_why_and_when_it_tries_again(self):
        result = version_check.check(now=1000.0, fetch=unreachable())
        self.assertIn("Could not check for a new version of DCCore: could not connect to GitHub (timed out). "
                      "Trying again tomorrow.", self.said())
        self.assertEqual(result["error"], "could not connect to GitHub (timed out)")
        self.assertEqual(result["error_at"], 1000.0)

    def test_the_failure_stays_on_show_until_a_check_succeeds(self):
        version_check.check(now=1000.0, fetch=unreachable())
        self.assertIn("could not check (could not connect to GitHub (timed out))", version_check.describe())
        version_check.check(now=2000.0, fetch=release("v1.13.0"))
        self.assertIsNone(version_check.state()["error"])
        self.assertEqual(version_check.describe(), "up to date (v1.13.0)")

    def test_a_manual_check_says_it_without_promising_tomorrow(self):
        version_check.manual_check(now=1000.0, fetch=unreachable("GitHub answered HTTP 502"))
        self.assertIn("Could not check for a new version of DCCore: GitHub answered HTTP 502.", self.said())
        self.assertNotIn("tomorrow", self.said())


class WhatACheckFinds(VersionCase):
    def test_a_newer_release_is_said_once_and_kept(self):
        version_check.check(now=1000.0, fetch=release("v1.14.0"))
        version_check.check(now=2000.0, fetch=release("v1.14.0"))
        self.assertEqual(self.said().count("A new version of DCCore is available: v1.14.0"), 1)
        self.assertIn("(this bot runs v1.13.0)", self.said())
        info = version_check.state()
        self.assertTrue(info["newer"])
        self.assertEqual(info["latest"], "v1.14.0")
        self.assertIn("v1.14.0 available", version_check.describe())

    def test_the_next_release_is_said_again(self):
        version_check.check(now=1000.0, fetch=release("v1.14.0"))
        version_check.check(now=2000.0, fetch=release("v1.15.0"))
        self.assertIn("available: v1.15.0", self.said())

    def test_the_same_or_older_release_is_no_news(self):
        for tag in ("v1.13.0", "v1.12.4"):
            version_check.check(now=1000.0, fetch=release(tag))
            self.assertFalse(version_check.state()["newer"], tag)
        self.assertEqual(self.feed, [])

    def test_a_prerelease_or_an_odd_tag_is_never_news(self):
        for latest in (release("v1.14.0", prerelease=True), release("v1.14.0-RC2"), release("nightly")):
            version_check.check(now=1000.0, fetch=latest)
            self.assertFalse(version_check.state()["newer"])
            self.assertIsNone(version_check.state()["latest"])
        self.assertEqual(self.feed, [])

    def test_a_release_candidate_hears_about_its_release(self):
        self.set_config(SCRIPT_VERSION="DCCore v1.14.0-RC1")
        version_check.check(now=1000.0, fetch=release("v1.14.0"))
        self.assertTrue(version_check.state()["newer"])


class CheckingByHand(VersionCase):
    def counting(self, fetch):
        self.calls = 0

        def counted():
            self.calls += 1
            return fetch()
        return counted

    def test_a_second_click_inside_a_minute_waits(self):
        fetch = self.counting(release("v1.13.0"))
        version_check.manual_check(now=1000.0, fetch=fetch)
        again = version_check.manual_check(now=1030.0, fetch=fetch)
        self.assertEqual(self.calls, 1)
        self.assertEqual(again["cooldown"], 31)
        self.assertEqual(again["current"], "v1.13.0", "the last outcome still comes back")
        version_check.manual_check(now=1061.0, fetch=fetch)
        self.assertEqual(self.calls, 2)

    def test_it_works_with_the_daily_check_off(self):
        self.set_config(CHECK_FOR_UPDATES=False)
        result = version_check.manual_check(now=1000.0, fetch=release("v1.14.0"))
        self.assertTrue(result["newer"])
        self.assertFalse(result["enabled"])


class TheDailyLoop(VersionCase):
    def test_due_once_a_day_and_never_when_off(self):
        self.assertTrue(version_check.due(1000.0))
        runtime.update_check_last_attempt = 1000.0
        self.assertFalse(version_check.due(1000.0 + version_check.CHECK_INTERVAL_SECONDS - 1))
        self.assertTrue(version_check.due(1000.0 + version_check.CHECK_INTERVAL_SECONDS))
        self.set_config(CHECK_FOR_UPDATES=False)
        self.assertFalse(version_check.due(10 ** 10))

    def test_the_worker_waits_then_checks_and_survives_a_crash(self):
        class Stop(Exception):
            pass

        naps = []

        def sleep(seconds):
            naps.append(seconds)
            if len(naps) == 3:
                raise Stop()

        def broken():
            raise RuntimeError("boom")

        original = version_check.fetch_latest
        self.addCleanup(setattr, version_check, "fetch_latest", original)
        version_check.fetch_latest = broken
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(Stop):
            version_check.worker(sleep=sleep)
        self.assertEqual(naps, [version_check.FIRST_CHECK_DELAY_SECONDS, version_check.WORKER_TICK_SECONDS,
                                version_check.WORKER_TICK_SECONDS])
        self.assertIn("[UPDATE] The version check failed unexpectedly: boom", output.getvalue())

    def test_off_starts_nothing(self):
        self.set_config(CHECK_FOR_UPDATES=False)
        started = []
        self.assertFalse(version_check.ensure_worker(start=lambda: started.append(1)))
        self.assertEqual(started, [])

    def test_on_starts_once(self):
        started = []
        self.assertTrue(version_check.ensure_worker(start=lambda: started.append(1)))
        self.assertFalse(version_check.ensure_worker(start=lambda: started.append(1)))
        self.assertEqual(started, [1])

    def test_boot_and_rehash_both_start_it(self):
        for name, statement in (("oserve.py", "version_check.ensure_worker()"),
                                ("commands.py", "if _version_check.ensure_worker():")):
            with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                self.assertIn(statement, handle.read(), name)

    def test_startup_says_it_is_on_and_how_to_turn_it_off(self):
        with io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            code = handle.read()
        self.assertIn('print("[UPDATE] Checking once a day for a new version of DCCore', code)
        self.assertIn("CHECK_FOR_UPDATES = false, or", code)

    def test_it_ships_on(self):
        with io.open(os.path.join(REPO_ROOT, "defaults.py"), encoding="utf-8") as handle:
            self.assertIn("\nCHECK_FOR_UPDATES: bool = True", handle.read())


class TheConsole(VersionCase):
    def test_checkversion_answers_in_the_session(self):
        original = version_check.fetch_latest
        self.addCleanup(setattr, version_check, "fetch_latest", original)
        version_check.fetch_latest = release("v1.14.0")
        session = FakeSession()
        adminchat.COMMANDS["checkversion"][0](session, "")
        deadline = time.time() + 10
        while len(session.lines) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(session.lines), 2, session.lines)
        self.assertEqual(session.lines[0], "Asking GitHub for the latest release...")
        self.assertIn("Version check: v1.14.0 available", session.lines[1])

    def test_checkversion_reports_a_failure(self):
        original = version_check.fetch_latest
        self.addCleanup(setattr, version_check, "fetch_latest", original)
        version_check.fetch_latest = unreachable("GitHub answered HTTP 502")
        session = FakeSession()
        adminchat.COMMANDS["checkversion"][0](session, "")
        deadline = time.time() + 10
        while len(session.lines) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertIn("could not check (GitHub answered HTTP 502)", session.text())

    def test_status_shows_the_version_line(self):
        version_check.check(now=time.time(), fetch=unreachable())
        session = FakeSession()
        adminchat.COMMANDS["status"][0](session, "")
        self.assertIn("Version     : could not check (could not connect to GitHub (timed out))", session.text())

    def test_the_mirc_menu_offers_it(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
            self.assertIn("  Check for a new version:dccore.send checkversion\r\n", handle.read())


class TheSetupPage(VersionCase):
    def by_name(self, values=None):
        return {f["name"]: f for f in webserver.build_setup_fields("en", values)}

    def test_a_fresh_page_has_it_ticked(self):
        self.set_config(CHECK_FOR_UPDATES=False)
        fields = webserver.build_setup_fields()
        self.assertTrue(self.by_name()["CHECK_FOR_UPDATES"]["value"])
        page = webserver.render_setup_page(fields, "token")
        self.assertIn('<input type="checkbox" name="CHECK_FOR_UPDATES" value="1" checked>', page)

    def test_a_redisplay_keeps_it_unticked(self):
        """An unticked box is absent from the POST; config ships True, and
        falling back to it would tick the box again behind the operator."""
        fields = webserver.build_setup_fields("en", {"NICKNAME": "Typed"})
        page = webserver.render_setup_page(fields, "token", values={"NICKNAME": "Typed"})
        self.assertIn('<input type="checkbox" name="CHECK_FOR_UPDATES" value="1">', page)

    def test_the_answer_is_written(self):
        form = {"NICKNAME": "MusicBot", "SERVER": "irc.example.net", "CHANNEL": "#example",
                "ADMIN_NICK": "SysOp", "password": "correct horse", "password_confirm": "correct horse"}
        changes, _hash, errors = webserver.validate_setup_form(dict(form, CHECK_FOR_UPDATES="1"))
        self.assertEqual(errors, [])
        self.assertIs(changes["CHECK_FOR_UPDATES"], True)
        changes, _hash, errors = webserver.validate_setup_form(form)
        self.assertIs(changes["CHECK_FOR_UPDATES"], False)


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt so these run there")
class TheDashboard(VersionCase):
    PASSWORD = "test-password"

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(self.PASSWORD, iterations=1000))
        self.client = webserver.create_app().test_client()
        webserver._web_bad_ips.clear()
        resp = self.client.post("/login", data={"password": self.PASSWORD})
        self.assertEqual(resp.status_code, 302, "the fixture's own login failed")
        original = version_check.fetch_latest
        self.addCleanup(setattr, version_check, "fetch_latest", original)
        version_check.fetch_latest = release("v1.14.0")

    def test_get_shows_what_was_found_without_asking(self):
        version_check.fetch_latest = lambda: self.fail("a page load must not ask GitHub")
        resp = self.client.get("/api/version-check")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["current"], "v1.13.0")

    def test_post_checks_now(self):
        resp = self.client.post("/api/version-check", json={})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["newer"])
        self.assertEqual(data["latest"], "v1.14.0")

    def test_both_need_the_login(self):
        anonymous = webserver.create_app().test_client()
        for method in (anonymous.get, anonymous.post):
            self.assertNotEqual(method("/api/version-check").status_code, 200)

    def test_the_sidebar_has_the_button(self):
        with io.open(os.path.join(REPO_ROOT, "web", "index.html"), encoding="utf-8") as handle:
            page = handle.read()
        self.assertIn('id="version-check-btn"', page)
        self.assertIn('id="version-text"', page)


if __name__ == "__main__":
    unittest.main()
