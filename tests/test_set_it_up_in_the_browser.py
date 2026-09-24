"""#547, Proposal 4: set it up in the browser, not the terminal.

With nothing configured and Flask present, oserve.startup() serves one
page on 127.0.0.1 until the form has written settings.conf and
admin_config.py exactly as configure.py writes them, then carries on down
the same line it always ran. Loopback only, one-shot, and behind a
one-time token in the URL, because with no password yet any website open
in the same browser could otherwise POST a password of its own.

The form is the Settings page's field machinery, so #545's plain help and
the fr/es strings appear without a second copy. The pure parts are tested
as functions; the app through Flask's test client; and the whole thing
end to end - a blank config, startup(), a real socket, a real POST, and
startup() continuing - over loopback where that is allowed.
"""

import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import configure  # noqa: E402
import defaults as config  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_adminchat import LOOPBACK_OK, NEEDS_LOOPBACK  # noqa: E402

NEEDS_FLASK = "Flask is not installed"
GOOD = {"NICKNAME": "MusicBot", "SERVER": "irc.example.net", "CHANNEL": "#example, #other",
        "ADMIN_NICK": "SysOp", "password": "correct horse", "password_confirm": "correct horse",
        "WEBUI_ENABLED": "1"}


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class TheForm(DCCoreTestCase):
    def test_the_fields_are_the_settings_pages_own(self):
        fields = webserver.build_setup_fields()
        self.assertEqual([f["name"] for f in fields], list(webserver.SETUP_FIELDS))
        by_name = {f["name"]: f for f in fields}
        self.assertEqual(by_name["NICKNAME"]["label"], webserver.SETTINGS_LABELS["NICKNAME"])
        self.assertIn("help", by_name["NICKNAME"], "#545's plain explanation")
        self.assertNotIn("settings_file.REQUIRED", by_name["NICKNAME"]["help"], "the plain one, not the developer's")

    def test_french_and_spanish_come_from_the_lang_files(self):
        for lang in ("fr", "es"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", f"{lang}.json"), encoding="utf-8") as handle:
                strings = json.load(handle)
            by_name = {f["name"]: f for f in webserver.build_setup_fields(lang)}
            self.assertEqual(by_name["NICKNAME"]["label"], strings["settings.field.NICKNAME"])
            self.assertEqual(by_name["NICKNAME"]["help"], strings["settings.field.NICKNAME.help"])

    def test_an_unknown_language_is_english(self):
        by_name = {f["name"]: f for f in webserver.build_setup_fields("xx")}
        self.assertEqual(by_name["NICKNAME"]["label"], webserver.SETTINGS_LABELS["NICKNAME"])

    def test_typed_values_are_redisplayed(self):
        by_name = {f["name"]: f for f in webserver.build_setup_fields("en", {"NICKNAME": "Typed"})}
        self.assertEqual(by_name["NICKNAME"]["value"], "Typed")


class Validation(DCCoreTestCase):
    def check(self, **overrides):
        form = dict(GOOD)
        form.update(overrides)
        return webserver.validate_setup_form(form)

    def test_a_good_form_is_the_dict_configure_builds(self):
        changes, password_hash, errors = self.check()
        self.assertEqual(errors, [])
        self.assertEqual(changes, {"NICKNAME": "MusicBot", "SERVER": "irc.example.net",
                                   "CHANNEL": "#example,#other", "ADMIN_NICK": "SysOp",
                                   "WEBUI_ENABLED": True, "WEBUI_HOST": "127.0.0.1",
                                   # This form does not send the version-check
                                   # box, which is how a browser sends it
                                   # unticked (#572).
                                   "CHECK_FOR_UPDATES": False})
        self.assertTrue(adminchat.verify_password(password_hash, "correct horse"))

    def test_what_was_left_blank_is_not_written(self):
        """configure.collect_answers()'s rule: a blank FILE_DIRECTORY is
        absent from the dict, not written as an empty value."""
        changes, _, errors = self.check(FILE_DIRECTORY="")
        self.assertEqual(errors, [])
        self.assertNotIn("FILE_DIRECTORY", changes)

    def test_the_dashboard_off_writes_no_host(self):
        changes, _, _ = self.check(WEBUI_ENABLED="")
        self.assertEqual(changes["WEBUI_ENABLED"], False)
        self.assertNotIn("WEBUI_HOST", changes)

    def test_lan_means_every_interface(self):
        changes, _, _ = self.check(WEBUI_LAN="1")
        self.assertEqual(changes["WEBUI_HOST"], "0.0.0.0")

    def test_each_refusal(self):
        cases = {
            "blank nick": dict(NICKNAME=""), "nick with a space": dict(NICKNAME="Music Bot"),
            "nick starting with a digit": dict(NICKNAME="9bot"),
            "blank server": dict(SERVER=""), "no channel": dict(CHANNEL=""),
            "channel without #": dict(CHANNEL="example"), "channel with a space": dict(CHANNEL="#ex ample"),
            "blank admin": dict(ADMIN_NICK=""), "blank password": dict(password="", password_confirm=""),
            "mismatch": dict(password_confirm="something else"),
            "missing folder": dict(FILE_DIRECTORY=os.path.join(tempfile.gettempdir(), "no-such-dccore-folder-x")),
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                _, _, errors = self.check(**overrides)
                self.assertEqual(len(errors), 1, (label, errors))

    def test_an_existing_folder_is_taken(self):
        tree = self.make_tree()
        changes, _, errors = self.check(FILE_DIRECTORY=tree.music)
        self.assertEqual(errors, [])
        self.assertEqual(changes["FILE_DIRECTORY"], tree.music)

    def test_errors_name_the_field_for_the_page(self):
        _, _, errors = self.check(NICKNAME="", password_confirm="x")
        self.assertEqual([name for name, _ in errors], ["NICKNAME", "password"])


class ApplyingIt(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-setup-apply-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = os.path.join(self.tmp, "settings.conf")
        self.admin = os.path.join(self.tmp, "admin_config.py")
        self.set_config(NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"], ADMIN_PASSWORD_HASH="")

    def test_it_writes_both_files_as_configure_does_and_applies_them(self):
        changes, password_hash, _ = webserver.validate_setup_form(GOOD)
        logs = []
        webserver.apply_setup(changes, password_hash, log=logs.append,
                              settings_path=self.settings, admin_path=self.admin)
        with io.open(self.settings, encoding="utf-8") as handle:
            conf = handle.read()
        self.assertIn("NICKNAME = MusicBot", conf)
        self.assertIn("CHANNEL = #example,#other", conf)
        with io.open(self.admin, encoding="utf-8") as handle:
            admin = handle.read()
        self.assertIn("ADMIN_PASSWORD_HASH", admin)
        self.assertIn(password_hash, admin)
        # and the running process sees them
        self.assertEqual(config.NICKNAME, "MusicBot")
        self.assertEqual(config.ADMIN_PASSWORD_HASH, password_hash)
        self.assertTrue(adminchat.password_is_configured())
        self.assertEqual(settings_file.unconfigured_required(vars(config), config.SHIPPED_DEFAULTS), [])

    def test_the_admin_file_is_the_terminal_paths_text(self):
        """Same writer, same text: configure.write_admin_config_password()."""
        _, password_hash, _ = webserver.validate_setup_form(GOOD)
        webserver.apply_setup({"NICKNAME": "X"}, password_hash, log=lambda *_: None,
                              settings_path=self.settings, admin_path=self.admin)
        expected = configure.build_admin_config_text(configure.NEW_ADMIN_CONFIG_HEADER,
                                                     password_hash)
        with io.open(self.admin, encoding="utf-8") as handle:
            got = handle.read()
        self.assertEqual(got.strip(), expected.strip())


class TheHostCheck(unittest.TestCase):
    def test_loopback_spellings_pass(self):
        for host in ("127.0.0.1:8420", "127.0.0.1", "localhost:8420", "LOCALHOST", "[::1]:8420"):
            self.assertTrue(webserver._setup_host_ok(host), host)

    def test_anything_else_is_refused(self):
        for host in ("evil.example", "192.168.1.5:8420", "dccore.local:8420", ""):
            self.assertFalse(webserver._setup_host_ok(host), host)


@unittest.skipUnless(webserver.HAVE_FLASK, NEEDS_FLASK)
class TheApp(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.done = []
        self.app = webserver.create_setup_app("the-token", self.done.append, port=8420)
        self.client = self.app.test_client()
        self.tmp = tempfile.mkdtemp(prefix="dccore-setup-app-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # the real writers are pointed at the temp folder
        self._real_apply = webserver.apply_setup
        webserver.apply_setup = lambda changes, h, log=print: self._real_apply(
            changes, h, log=lambda *_: None,
            settings_path=os.path.join(self.tmp, "settings.conf"),
            admin_path=os.path.join(self.tmp, "admin_config.py"))
        self.addCleanup(setattr, webserver, "apply_setup", self._real_apply)
        self.set_config(NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"], ADMIN_PASSWORD_HASH="")

    def get(self, path, host="127.0.0.1:8420"):
        return self.client.get(path, headers={"Host": host})

    def post(self, data, host="127.0.0.1:8420"):
        return self.client.post("/setup", data=data, headers={"Host": host})

    def test_the_page_needs_the_token(self):
        self.assertEqual(self.get("/setup").status_code, 403)
        self.assertEqual(self.get("/setup?token=wrong").status_code, 403)
        self.assertEqual(self.get("/setup?token=the-token").status_code, 200)

    def test_a_non_ascii_token_is_refused_not_a_crash(self):
        """The token is compared in constant time, on bytes: a value that is
        not ASCII in the URL must be one more wrong token, not a 500."""
        self.assertEqual(self.get("/setup?token=t%C3%B6ken").status_code, 403)
        self.assertEqual(self.post({**GOOD, "token": "t\u00f6ken"}).status_code, 403)

    def test_root_redirects_to_the_page_with_the_token(self):
        response = self.get("/?token=the-token")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/setup?token=the-token", response.headers["Location"])

    def test_a_foreign_host_header_is_refused(self):
        self.assertEqual(self.get("/setup?token=the-token", host="evil.example:8420").status_code, 403)

    def test_the_page_carries_every_field_the_help_and_the_token(self):
        html = self.get("/setup?token=the-token").get_data(as_text=True)
        for name in webserver.SETUP_FIELDS:
            if name != "WEBUI_HOST":
                self.assertIn(f'name="{name}"', html, name)
        self.assertIn('name="WEBUI_LAN"', html)
        self.assertIn('name="password_confirm"', html)
        self.assertIn('name="token" value="the-token"', html)
        self.assertIn('class="help"', html)
        self.assertIn("irc.undernet.org", html, "the usual server is prefilled")
        self.assertNotIn("<script", html, "no script on the form page")

    def test_french(self):
        html = self.get("/setup?token=the-token&lang=fr").get_data(as_text=True)
        self.assertIn("Pseudo", html)
        self.assertIn('lang="fr"', html)

    def test_a_post_without_the_token_is_refused_even_if_complete(self):
        """The CSRF case: a page from another origin can build this POST;
        it cannot know the token."""
        self.assertEqual(self.post(dict(GOOD)).status_code, 403)
        self.assertEqual(self.done, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "settings.conf")))

    def test_a_bad_form_comes_back_with_its_errors_and_values(self):
        data = dict(GOOD, token="the-token", NICKNAME="", CHANNEL="example")
        response = self.post(data)
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count('class="error"'), 2)
        self.assertIn('value="irc.example.net"', html, "what was typed is redisplayed")
        self.assertNotIn("correct horse", html, "never the password")
        self.assertEqual(self.done, [])

    def test_a_good_form_writes_reports_and_tells_the_page_to_wait_for_the_dashboard(self):
        response = self.post(dict(GOOD, token="the-token"))
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Saved", html)
        self.assertIn("if (true)", html, "the page polls for the dashboard")
        self.assertEqual(len(self.done), 1)
        self.assertEqual(self.done[0]["NICKNAME"], "MusicBot")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "settings.conf")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "admin_config.py")))

    def test_with_the_dashboard_off_the_page_does_not_poll(self):
        html = self.post(dict(GOOD, token="the-token", WEBUI_ENABLED="")).get_data(as_text=True)
        self.assertIn("if (false)", html)
        self.assertIn("Close this tab", html)

    def test_after_saving_the_page_is_the_saved_page_and_the_poll_gets_503(self):
        self.post(dict(GOOD, token="the-token"))
        self.assertIn("Saved", self.get("/setup").get_data(as_text=True))
        self.assertEqual(self.get("/login").status_code, 503)

    def test_a_second_post_after_saving_does_not_write_again(self):
        self.post(dict(GOOD, token="the-token"))
        self.post(dict(GOOD, token="the-token", NICKNAME="Other"))
        self.assertEqual(len(self.done), 1)


@unittest.skipUnless(webserver.HAVE_FLASK, NEEDS_FLASK)
class TheServer(DCCoreTestCase):
    """run_setup_until_configured() over a real socket."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-setup-srv-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._real_apply = webserver.apply_setup
        webserver.apply_setup = lambda changes, h, log=print: self._real_apply(
            changes, h, log=lambda *_: None,
            settings_path=os.path.join(self.tmp, "settings.conf"),
            admin_path=os.path.join(self.tmp, "admin_config.py"))
        self.addCleanup(setattr, webserver, "apply_setup", self._real_apply)
        self.set_config(NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"], ADMIN_PASSWORD_HASH="",
                        WEBUI_OPEN_BROWSER=True)

    @unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
    def test_it_serves_until_the_form_is_saved_then_frees_the_port(self):
        port = free_port()
        logs, opened = [], []
        result = {}

        def run():
            result["changes"] = webserver.run_setup_until_configured(
                port=port, log=logs.append, opener=opened.append, token="tok")

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not opened:
            time.sleep(0.02)
        self.assertEqual(opened, [f"http://127.0.0.1:{port}/setup?token=tok"], "the browser is pointed at the token URL")
        self.assertTrue(any("/setup?token=tok" in line for line in logs), "and the URL is printed")

        # One browser, which keeps its cookies: the code is bound to the
        # first browser that presents it (#675), and a bare urlopen() per
        # request would look like a new browser each time.
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        # the page is up
        page = browser.open(f"http://127.0.0.1:{port}/setup?token=tok", timeout=5).read().decode()
        self.assertIn('name="NICKNAME"', page)
        # a post without the token: refused
        with self.assertRaises(urllib.error.HTTPError) as caught:
            browser.open(f"http://127.0.0.1:{port}/setup",
                         data=urllib.parse.urlencode(GOOD).encode(), timeout=5)
        self.assertEqual(caught.exception.code, 403)
        # the code from ps, in another browser: refused too
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/setup?token=tok", timeout=5)
        self.assertEqual(caught.exception.code, 403)
        # the real thing
        body = urllib.parse.urlencode(dict(GOOD, token="tok")).encode()
        saved = browser.open(f"http://127.0.0.1:{port}/setup", data=body, timeout=5).read().decode()
        self.assertIn("Saved", saved)

        thread.join(10)
        self.assertFalse(thread.is_alive(), "the server returns once the form is saved")
        self.assertEqual(result["changes"]["NICKNAME"], "MusicBot")
        self.assertTrue(any("Settings written" in line for line in logs))
        # and the port is free for the real dashboard - which binds with
        # SO_REUSEADDR (werkzeug's BaseWSGIServer.allow_reuse_address), so
        # the probe does too: on Linux a just-closed listener still has this
        # test's own connections in TIME_WAIT, and a plain bind refuses that
        # for a minute while a reusing one, the real one, does not.
        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.settimeout(2)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                probe.bind(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("the port was not released")
        probe.close()

    @unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
    def test_giving_up_returns_none(self):
        """Ctrl-C's path: the wait loop asked to stop before any form."""
        port = free_port()
        asked = []

        def give_up():
            asked.append(1)
            return True

        result = webserver.run_setup_until_configured(port=port, log=lambda *_: None,
                                                      opener=lambda *_: None, wait=give_up, token="tok")
        self.assertIsNone(result)
        self.assertTrue(asked)

    @unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
    def test_a_taken_port_is_reported_not_fatal(self):
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        port = holder.getsockname()[1]
        logs = []
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):     # werkzeug's own "port in use" line
            result = webserver.run_setup_until_configured(port=port, log=logs.append, opener=lambda *_: None)
        self.assertIsNone(result)
        self.assertTrue(any("Could not open the setup page" in line for line in logs))
        # #617: werkzeug's SystemExit used to print as "(1)", the cause and the
        # ways out unsaid - a first-timer with another DCCore minimised saw
        # nothing to do. The cause is named, and so are the ways out.
        joined = "\n".join(logs)
        self.assertIn(f"{port} - the port is taken", joined)
        self.assertIn("another DCCore", joined)
        self.assertIn("WEBUI_PORT", joined)
        self.assertIn("configure.py", joined)
        self.assertNotIn("(1)", joined)

    def test_a_taken_port_never_exits_the_process(self):
        """werkzeug's server calls sys.exit(1) on a bind failure; the daemon
        must not inherit that - pinned on the source since the test above
        would simply die if it did."""
        source = io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8").read()
        body = source.split("def run_setup_until_configured(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("except (OSError, SystemExit)", body)

    @unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
    def test_the_browser_is_not_opened_when_the_operator_said_not_to(self):
        """The False branch, executed (#646, audit M44): the server is
        started with the flag off and an opener that records, told to give
        up as soon as it has said what it says instead of opening one, and
        the opener must never have been called. The source pin below used
        to be the only test of this branch, and moving the opener call
        outside the guard passed it."""
        self.set_config(WEBUI_OPEN_BROWSER=False)
        port = free_port()
        logs, opened = [], []
        said_so = threading.Event()

        def log(line):
            logs.append(line)
            if "No browser was opened here" in line:
                said_so.set()

        result = {}

        def run():
            result["changes"] = webserver.run_setup_until_configured(
                port=port, log=log, opener=opened.append, token="tok",
                wait=said_so.is_set)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(15)

        self.assertFalse(thread.is_alive(), "the server did not give up when told to")
        self.assertTrue(said_so.is_set(), logs)
        self.assertEqual(opened, [], "the browser was opened with WEBUI_OPEN_BROWSER off")
        self.assertIsNone(result["changes"])
        joined = "\n".join(logs)
        self.assertIn("/setup?token=tok", joined, "the link is still printed for the operator to open")
        self.assertIn("ssh -L", joined, "and the way to reach it over SSH")

    def test_the_decision_reads_the_setting_and_nothing_else(self):
        """The everywhere half of the test above, which needs loopback: the
        guard is the setting, read with the shipped default of True."""
        source = io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8").read()
        body = source.split("def run_setup_until_configured(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('if getattr(config, "WEBUI_OPEN_BROWSER", True):', body)


class StartupUsesIt(DCCoreTestCase):
    """oserve.startup()'s new branch, with the page stood in for."""

    def setUp(self):
        super().setUp()
        import importlib
        sys.modules.pop("oserve", None)
        self.oserve = importlib.import_module("oserve")
        tree = self.make_tree()
        self.set_config(FILE_DIRECTORY=tree.music, LOCAL_LIST_DIR=tree.lists,
                        BANS_FILE=os.path.join(tree.root, "bans.txt"),
                        DCC_QUEUE_FILE=os.path.join(tree.root, "dcc_queue.txt"),
                        SERVER="irc.test.example", DEBUG_CHANNEL="#test-debug",
                        NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"],
                        CHANNEL=config.SHIPPED_DEFAULTS["CHANNEL"],
                        ADMIN_NICK=config.SHIPPED_DEFAULTS["ADMIN_NICK"])
        import queue_mgr
        self._real_worker = queue_mgr.queue_worker
        queue_mgr.queue_worker = lambda: None
        self.addCleanup(setattr, queue_mgr, "queue_worker", self._real_worker)
        # And the fetch dispatcher (#799) - see test_startup.BootCase.
        import dcc_fetch
        self._real_dispatcher = dcc_fetch.fetch_dispatcher_worker
        dcc_fetch.fetch_dispatcher_worker = lambda: None
        self.addCleanup(setattr, dcc_fetch, "fetch_dispatcher_worker", self._real_dispatcher)

    def boot(self, **kwargs):
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.oserve.startup(**kwargs)
        return buffer.getvalue()

    def test_a_blank_config_serves_the_page_then_carries_on(self):
        served = []

        def page():
            served.append(1)
            config.NICKNAME, config.CHANNEL, config.ADMIN_NICK = "MusicBot", "#example", "SysOp"

        output = self.boot(setup_page=page)
        self.assertEqual(served, [1])
        self.assertIn("opening the setup page", output)
        self.assertIn(config.SCRIPT_VERSION, output)
        self.assertNotIn("[CRITICAL]", output)

    def test_a_page_that_returns_without_configuring_still_refuses(self):
        """#617: a distinct exit code, and the terminal questions named -
        this is a first run whose page could not finish (a taken port, or
        Ctrl-C), and "copy the sample" is the step the launchers exist to
        spare a first-timer. The launchers map the code to asking them."""
        buffer = io.StringIO()
        import contextlib
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(buffer):
            self.oserve.startup(setup_page=lambda: None)
        self.assertEqual(caught.exception.code, self.oserve.EXIT_SETUP_IN_THE_TERMINAL)
        self.assertEqual(caught.exception.code, 3)
        output = buffer.getvalue()
        self.assertIn("[CRITICAL]", output)
        self.assertIn("configure.py", output)
        self.assertNotIn(".sample", output)

    def test_setup_page_false_is_the_old_refusal(self):
        """No page was tried (no Flask): exit 1 and the refusal, as before -
        that code means "stop" to the launchers, not "ask". The refusal
        names the launcher and configure.py, not the sample files (#685)."""
        buffer = io.StringIO()
        import contextlib
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(buffer):
            self.oserve.startup(setup_page=False)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("[CRITICAL] Run the launcher (start-dccore", buffer.getvalue())
        self.assertIn("configure.py", buffer.getvalue())

    def test_the_page_is_possible_exactly_when_flask_is(self):
        """No setting turns it off: loopback and one-shot by design."""
        self.assertEqual(webserver.setup_page_is_possible(), webserver.HAVE_FLASK)
        real = webserver.HAVE_FLASK
        webserver.HAVE_FLASK = False
        try:
            self.assertFalse(webserver.setup_page_is_possible())
        finally:
            webserver.HAVE_FLASK = real

    def test_the_default_is_the_real_page_when_flask_is_there(self):
        source = io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8").read()
        body = source.split("def startup(", 1)[1]
        self.assertIn("webserver.run_setup_until_configured", body)
        self.assertIn("webserver.setup_page_is_possible()", body)


class TheLauncherHook(DCCoreTestCase):
    """configure.py --setup-in-browser: 0 = start the daemon, it serves the
    page; 2 = ask here."""

    def test_with_flask_it_is_a_yes_without_asking(self):
        if not webserver.HAVE_FLASK:
            raise unittest.SkipTest(NEEDS_FLASK)
        asked = []
        self.assertEqual(configure.offer_setup_in_browser(ask=lambda p: asked.append(p) or "", log=lambda *_: None, environ={}), 0)
        self.assertEqual(asked, [])

    def test_nobody_at_the_keyboard_is_a_no(self):
        """systemd, a closed stdin: the questions path, which the launcher
        then finds equally unanswerable and reports - never a hang here."""
        def eof(prompt):
            raise EOFError
        real = sys.modules.get("flask")
        sys.modules["flask"] = None      # import fails
        try:
            self.assertEqual(configure.offer_setup_in_browser(ask=eof, log=lambda *_: None), 2)
        finally:
            if real is None:
                sys.modules.pop("flask", None)
            else:
                sys.modules["flask"] = real

    def test_a_no_is_the_questions(self):
        real = sys.modules.get("flask")
        sys.modules["flask"] = None
        try:
            self.assertEqual(configure.offer_setup_in_browser(ask=lambda p: "n", log=lambda *_: None), 2)
        finally:
            if real is None:
                sys.modules.pop("flask", None)
            else:
                sys.modules["flask"] = real

    def test_the_flag_is_wired_in_configure_main(self):
        source = io.open(os.path.join(REPO_ROOT, "configure.py"), encoding="utf-8").read()
        self.assertIn('if "--setup-in-browser" in sys.argv[1:]:', source)
        self.assertIn("sys.exit(offer_setup_in_browser())", source)


class TheDocs(unittest.TestCase):
    def test_the_guides_say_so(self):
        for name in ("INSTALL.md", "WINDOWS.md"):
            with io.open(os.path.join(REPO_ROOT, "docs", name), encoding="utf-8") as handle:
                doc = handle.read()
            self.assertIn("/setup", doc, name)
            self.assertIn("browser", doc, name)


if __name__ == "__main__":
    unittest.main()
