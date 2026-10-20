"""A request that lands while a rehash reloads the settings keeps its session (#1433).

A rehash re-executes defaults.py, which puts ADMIN_PASSWORD_HASH back to ""
until settings.conf is applied again, about a millisecond later (see
runtime.config_reload_lock). The dashboard compared each session's password
stamp against that value without the lock, so a request in the window looked
like one from before a password change: 401, the cookie cleared, the browser
sent to /login. The likeliest such request is the Settings page's own
re-fetch right after a save. A login checked in the window was refused and
counted as a failed attempt.

The window is reproduced here as the rehash makes it: a thread holds the lock
while the hash is "", then puts it back - before a request, and part-way
through one (a login's new session, a password change's re-stamp). The last
class sends the request from inside a real reload of defaults.py.
"""

import contextlib
import io
import os
import sys
import threading
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_every_route_is_behind_the_login import PASSWORD, RouteCase  # noqa: E402

# Long enough that a request started inside the window is still inside it
# when it reads the hash, unless it waits for the lock.
WINDOW_SECONDS = 0.3


class ReloadWindow:
    """What a rehash does to the hash, on its own thread: under the lock, the
    hash is "" for WINDOW_SECONDS, then the configured one again."""

    def __init__(self):
        self.opened = threading.Event()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with runtime.config_reload_lock:
            configured = config.ADMIN_PASSWORD_HASH
            config.ADMIN_PASSWORD_HASH = ""
            self.opened.set()
            threading.Event().wait(WINDOW_SECONDS)
            config.ADMIN_PASSWORD_HASH = configured
            # Set while the lock is still held: whoever got the lock after
            # this thread sees it set, whoever read without it may not.
            self.closed.set()

    def __enter__(self):
        self.thread.start()
        if not self.opened.wait(5):
            raise AssertionError("the reload window never opened")
        return self

    def __exit__(self, *exc):
        self.thread.join(5)


class TheHashIsReadUnderTheLock(DCCoreTestCase):
    """_admin_password_hash() itself - no Flask."""

    def test_it_waits_for_the_reload_and_reads_the_configured_hash(self):
        self.set_config(ADMIN_PASSWORD_HASH="pbkdf2$configured")

        with ReloadWindow() as window:
            got = webserver._admin_password_hash()

        self.assertEqual(got, "pbkdf2$configured")
        # Not a timing check: closed is set under the lock, so a read that
        # waited for the lock always sees it, however slow the runner.
        self.assertTrue(window.closed.is_set(),
                        "the hash was read before the reload window closed")


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class ARequestDuringTheReload(RouteCase):

    def test_an_open_session_stays_open(self):
        self.log_in()

        with ReloadWindow():
            resp = self.client.get("/api/settings")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/api/settings").status_code, 200,
                         "the session was dropped by the request in the window")

    def test_a_correct_password_logs_in_and_is_not_counted_as_a_failure(self):
        with ReloadWindow():
            resp = self.client.post("/login", data={"password": PASSWORD})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        self.assertEqual(dict(webserver._web_bad_ips), {})


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class AReloadThatStartsDuringTheRequest(RouteCase):
    """The window opening part-way through a request, after its own check:
    the session it opens, or re-stamps after a password change, must carry
    the configured hash and not the reload's "". Stamped with "", the next
    request no longer matches and the operator is logged out by a login
    that worked, or by the password change they just made."""

    def open_window_after(self, name):
        """Replace webserver.<name> with one that, once the real one has
        returned, opens a ReloadWindow - a rehash reaching its reload at
        that moment. The window closes on its own; wait_for_the_window()
        waits for that."""
        real = getattr(webserver, name)
        self.windows = []

        def then_a_reload(*args, **kwargs):
            result = real(*args, **kwargs)
            window = ReloadWindow().__enter__()
            self.windows.append(window)
            self.addCleanup(window.__exit__)
            return result

        setattr(webserver, name, then_a_reload)
        self.addCleanup(setattr, webserver, name, real)

    def wait_for_the_window(self):
        """The next request is checked against the configured hash, as it
        will be from now on: one sent while the hash is still "" would match
        a session wrongly stamped with "" and hide it."""
        self.assertEqual(len(self.windows), 1, "control: the reload window never opened")
        self.windows[0].__exit__()
        self.assertTrue(self.windows[0].closed.is_set())

    def test_a_session_opened_in_the_window_works(self):
        self.open_window_after("_check_web_password")

        resp = self.client.post("/login", data={"password": PASSWORD})

        self.assertEqual(resp.status_code, 302)
        self.wait_for_the_window()
        self.assertEqual(self.client.get("/api/settings").status_code, 200,
                         "the login's session was stamped with the reload's blank hash")

    def test_the_tab_that_changed_the_password_stays_logged_in(self):
        # The change dispatches a real rehash otherwise; the window stands in
        # for it, at the moment that hurts.
        real_rehash = commands.handle_rehash_request
        commands.handle_rehash_request = lambda *a, **kw: None
        self.addCleanup(setattr, commands, "handle_rehash_request", real_rehash)
        self.log_in()
        self.open_window_after("build_password_change_result")

        resp = self.client.post("/api/settings/password", json={
            "new_password": "a-new-one-entirely", "confirm_password": "a-new-one-entirely"})

        self.assertEqual(resp.status_code, 200)
        self.wait_for_the_window()
        self.assertEqual(self.client.get("/api/settings").status_code, 200,
                         "the tab was re-stamped with the reload's blank hash")


# Long enough that a request which does not wait for the lock has finished
# inside the reload; one that does wait is held there however long this is.
IN_THE_WINDOW_SECONDS = 0.5


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class ARequestDuringARealReload(RouteCase):
    """The same window made by the real thing rather than by ReloadWindow:
    commands.reload_modules_in_order() re-executes defaults.py, and the
    request is sent from inside it, as defaults.py is about to apply
    settings.conf - the moment the hash is defaults.py's own ""."""

    def setUp(self):
        super().setUp()
        # The reload puts every setting back to defaults.py plus this
        # test's settings.conf (DCCoreTestCase points it at a temp file).
        self.keep_every_setting()
        self.set_config(_SETTINGS_REPORT=getattr(config, "_SETTINGS_REPORT", None))
        # An empty admin_config for the reload's `from admin_config import *`:
        # a checkout whose conf/admin_config.py sets ADMIN_PASSWORD_HASH would
        # otherwise fill the window before the request is sent, and the
        # control below would fail for a reason that is not the code's.
        # Only this one entry is put back - patch.dict(sys.modules) would
        # also drop every module the reload imports for the first time.
        had_admin_config = "admin_config" in sys.modules
        previous_admin_config = sys.modules.get("admin_config")
        sys.modules["admin_config"] = types.ModuleType("admin_config")
        self.addCleanup(
            lambda: sys.modules.__setitem__("admin_config", previous_admin_config)
            if had_admin_config else sys.modules.pop("admin_config", None))
        settings_file.save(vars(config),
                           {"ADMIN_PASSWORD_HASH": config.ADMIN_PASSWORD_HASH},
                           log=lambda *a, **kw: None)

    def during_the_reload(self, send):
        """send() on its own thread from inside a real reload; its response."""
        seen, outcome = {}, {}

        def worker():
            try:
                outcome["response"] = send()
            except BaseException as err:  # reported below, not lost on the thread
                outcome["error"] = err

        real_apply_to = settings_file.apply_to

        def apply_to(namespace, *args, **kwargs):
            seen["hash"] = namespace.get("ADMIN_PASSWORD_HASH")
            thread = threading.Thread(target=worker, daemon=True)
            seen["thread"] = thread
            thread.start()
            thread.join(IN_THE_WINDOW_SECONDS)
            return real_apply_to(namespace, *args, **kwargs)

        settings_file.apply_to = apply_to
        try:
            # Quietly: the reload reports what it applied and kept.
            with contextlib.redirect_stdout(io.StringIO()):
                commands.reload_modules_in_order(modules=("defaults",), reload_self=False)
        finally:
            settings_file.apply_to = real_apply_to

        self.assertIn("thread", seen, "the reload never applied settings.conf")
        seen["thread"].join(10)
        self.assertEqual(seen["hash"], "",
                         "control: the request was not sent inside the reload's window")
        if "error" in outcome:
            raise outcome["error"]
        self.assertIn("response", outcome, "the request never finished")
        return outcome["response"]

    def test_an_open_session_stays_open(self):
        self.log_in()

        resp = self.during_the_reload(lambda: self.client.get("/api/settings"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/api/settings").status_code, 200)

    def test_a_correct_password_logs_in_and_is_not_counted_as_a_failure(self):
        resp = self.during_the_reload(
            lambda: self.client.post("/login", data={"password": PASSWORD}))

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        self.assertEqual(dict(webserver._web_bad_ips), {})


if __name__ == "__main__":
    unittest.main()
