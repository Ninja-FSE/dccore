"""Behavioural coverage for functions the suite never entered.

The pre-publication audit's first critical: a set of public daemon functions
had no behavioural coverage at all. Not thin coverage - none. Their bodies
could be replaced with `raise` and the whole suite stayed green.

scripts/function_coverage.py now measures this on every preflight run and fails
on any public function nothing enters. This file is the first instalment of
paying the debt down; tests/uncovered_functions.txt holds what is left, with a
reason for each.

These are entry-level tests, and deliberately so. A function that nothing calls
has no regression protection whatsoever, and the first thing worth having is
one caller that would notice it disappearing. Several are richer than that
because the function turned out to be interesting once looked at.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import list as list_mod  # noqa: E402
import runtime  # noqa: E402
import update_list  # noqa: E402

from tests.exported_tree import _this_is_an_export  # noqa: E402
from tests.support import DCCoreTestCase, RecordingSocket, temp_dir  # noqa: E402


class HumanReadableSizes(unittest.TestCase):
    """update_list.format_size_human / format_total_size.

    Two nearly identical functions differing only in whether they count in
    powers of 1024 labelled as KB or as KiB. Nothing called either, so nothing
    would have noticed them being swapped - and the advert publishes one of
    them into every channel.
    """

    def test_bytes_stay_bytes(self):
        self.assertEqual(update_list.format_size_human(0), "0.0B")
        self.assertEqual(update_list.format_size_human(999), "999.0B")

    def test_it_steps_up_at_1024_not_1000(self):
        self.assertEqual(update_list.format_size_human(1024), "1.0KB")
        self.assertEqual(update_list.format_size_human(1023), "1023.0B")

    def test_each_unit_in_turn(self):
        for power, unit in enumerate(["B", "KB", "MB", "GB", "TB"]):
            with self.subTest(unit=unit):
                self.assertTrue(
                    update_list.format_size_human(1024 ** power).endswith(unit))

    def test_past_the_last_unit_it_stays_in_petabytes(self):
        """The loop falls out rather than running off the end of the list."""
        self.assertTrue(update_list.format_size_human(1024 ** 6).endswith("PB"))

    def test_the_binary_variant_uses_the_binary_labels(self):
        """The whole difference between the two, and the reason both exist."""
        self.assertEqual(update_list.format_total_size(1024), "1.0KiB")
        self.assertEqual(update_list.format_total_size(1024 ** 5), "1.0PiB")

    def test_the_two_agree_on_the_number_and_differ_on_the_label(self):
        for size in (0, 1023, 1024, 1024 ** 2, 1024 ** 3):
            with self.subTest(size=size):
                human = update_list.format_size_human(size)
                binary = update_list.format_total_size(size)
                self.assertEqual(human.rstrip("BKMGTPi"),
                                 binary.rstrip("BKMGTPi"))


class TheReadLoopGuard(unittest.TestCase):
    """irc.never_breaks_the_read_loop.

    Its own docstring says a stranger who can make an observational capture
    raise can hold the daemon in a reconnect loop for as long as they keep
    typing - unauthenticated, unmetered, and with a ban unable to stop it.
    The decorator that removes that whole class of attack had no test.
    """

    def test_a_raising_capture_does_not_propagate(self):
        @irc.never_breaks_the_read_loop
        def explodes(line):
            raise ValueError("a malformed number in a SLOTS line")

        explodes("anything")  # must not raise

    def test_every_exception_type_is_contained(self):
        """A stranger picks the exception by picking the input, so catching a
        chosen few would leave the hole open."""
        for problem in (ValueError, KeyError, IndexError, TypeError,
                        AttributeError, ZeroDivisionError, UnicodeDecodeError):
            with self.subTest(problem=problem.__name__):
                @irc.never_breaks_the_read_loop
                def explodes(line):
                    if problem is UnicodeDecodeError:
                        raise UnicodeDecodeError("utf-8", b"", 0, 1, "bad")
                    raise problem("boom")

                explodes("anything")

    def test_a_working_capture_still_runs_and_returns(self):
        """Control. A decorator that swallowed the call as well as the
        exception would pass both tests above."""
        seen = []

        @irc.never_breaks_the_read_loop
        def captures(line):
            seen.append(line)
            return "returned"

        result = captures("SLOTS 3/5")

        self.assertEqual(seen, ["SLOTS 3/5"])
        self.assertEqual(result, "returned")

    def test_it_keeps_the_wrapped_functions_identity(self):
        """functools.wraps, so a traceback still names the real function."""
        @irc.never_breaks_the_read_loop
        def capture_slots(line):
            return None

        self.assertEqual(capture_slots.__name__, "capture_slots")

    def test_the_next_line_is_still_processed(self):
        """The point of the whole thing: one unparseable line costs exactly
        that line, not the connection."""
        seen = []

        @irc.never_breaks_the_read_loop
        def flaky(line):
            if line == "bad":
                raise ValueError("nope")
            seen.append(line)

        flaky("good one")
        flaky("bad")
        flaky("good two")

        self.assertEqual(seen, ["good one", "good two"])


class TheRealQueueMessage(unittest.TestCase):
    """oserve.queue_message - the real one.

    Every other test in the suite installs a stub oserve, because importing the
    real module used to start worker threads. It does not any more (startup()
    and run_forever() were split out for exactly this reason), so the VIP gate
    that decides whether a message skips flood protection can be driven.
    """

    def setUp(self):
        # Loaded from the file under its own name rather than `import oserve`.
        # Every other test in the suite installs a stub at sys.modules["oserve"],
        # so a plain import here picks up whichever ran first - and the stub is
        # exactly the thing this class exists to not use. The module-level state
        # that matters (config.vip_queue, config.send_queue) lives in defaults,
        # which is shared, so a second copy of oserve queues into the same
        # containers the real one does.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "oserve_under_test", os.path.join(REPO_ROOT, "oserve.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.oserve = module
        self.config = config
        config.vip_queue.clear()
        config.send_queue.clear()
        self.addCleanup(config.vip_queue.clear)
        self.addCleanup(config.send_queue.clear)

    def test_an_ordinary_message_goes_to_that_users_queue(self):
        self.oserve.queue_message("Dave", "NOTICE Dave :hello\r\n")

        self.assertEqual(config.send_queue["dave"], ["NOTICE Dave :hello\r\n"])
        self.assertEqual(list(config.vip_queue), [])

    def test_the_user_key_is_lowercased(self):
        """IRC nicks are case-insensitive; two spellings must not become two
        queues, or a user's -remove misses half their own files."""
        self.oserve.queue_message("Dave", "one\r\n")
        self.oserve.queue_message("dAVe", "two\r\n")

        self.assertEqual(len(config.send_queue["dave"]), 2)

    def test_an_explicit_vip_message_skips_the_per_user_queue(self):
        self.oserve.queue_message("Dave", "urgent\r\n", is_vip=True)

        self.assertIn("urgent\r\n", list(config.vip_queue))
        self.assertNotIn("dave", config.send_queue)

    def test_a_channel_advert_is_vip_without_asking(self):
        """The advert is not addressed to a user, so it must not sit in one
        user's flood-limited queue waiting behind their transfers."""
        self.oserve.queue_message("CHANNEL_ANNOUNCE", "PRIVMSG #chan :advert\r\n")

        self.assertIn("PRIVMSG #chan :advert\r\n", list(config.vip_queue))
        self.assertNotIn("channel_announce", config.send_queue)


class TheDccErrorNotice(DCCoreTestCase):
    """announce.send_dcc_error - the only thing that tells a user why their
    request was refused."""

    def sent(self):
        return "".join(m for _u, m, *_ in self.oserve.queued)

    def test_each_known_error_produces_its_own_message(self):
        seen = set()
        for kind in ("invalid_path", "file_not_found", "global_full",
                     "user_full", "rar_disabled", "not_configured"):
            with self.subTest(error=kind):
                self.oserve.queued.clear()
                announce.send_dcc_error("dave", kind)
                text = self.sent()

                self.assertIn("NOTICE dave", text)
                seen.add(text)

        self.assertEqual(len(seen), 6, "two error kinds share one message")

    def test_an_unknown_error_still_says_something(self):
        """A refusal with no message is a request that vanishes."""
        announce.send_dcc_error("dave", "no_such_error_kind")

        self.assertIn("Unknown transfer issue", self.sent())

    def test_it_is_a_complete_irc_command(self):
        """queue_message payloads go straight to the socket. A bare text line
        gets answered with "421 Unknown command" and the user sees nothing -
        which has happened here before, in send_file_list."""
        announce.send_dcc_error("dave", "file_not_found")
        text = self.sent()

        self.assertTrue(text.startswith("NOTICE "))
        self.assertTrue(text.endswith("\r\n"))


class TheQueueCheckCommand(DCCoreTestCase):
    """commands.handle_queue_check - the -que command, one of the two most
    common things a user types at this bot."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, NICKNAME="TestBot",
                        LIST_BASE_NAME="TestBot")

    def ask(self, user="dave"):
        self.oserve.queued.clear()
        commands.handle_queue_check(RecordingSocket(), user, "#chan")
        return "".join(m for _u, m, *_ in self.oserve.queued)

    def test_an_empty_queue_is_answered_with_zero(self):
        text = self.ask()

        self.assertIn("NOTICE dave", text)
        self.assertIn("0", text)

    def test_a_queued_user_is_told_how_many(self):
        config.dcc_queue["dave"] = [{"file": "a.flac"}, {"file": "b.flac"}]
        self.addCleanup(config.dcc_queue.clear)

        self.assertIn("2", self.ask())

    def test_it_answers_by_notice_and_not_into_the_channel(self):
        """A stranger looking the bot over must cost the channel nothing."""
        text = self.ask()

        self.assertNotIn("PRIVMSG #chan", text)

    def test_the_lookup_is_case_insensitive(self):
        config.dcc_queue["dave"] = [{"file": "a.flac"}]
        self.addCleanup(config.dcc_queue.clear)

        self.assertIn("1", self.ask(user="DAVE"))


class ThePingCommand(DCCoreTestCase):
    """commands.handle_ping_request / handle_pong_response - a matched pair
    that communicate through three module-level names on config."""

    def setUp(self):
        super().setUp()
        # "operator" pings below: !ping answers only the bot's own admin now
        # (tests/test_diagnostics_answer_only_the_admin.py); what this class
        # covers is what happens once a ping IS accepted.
        self.set_config(ADMIN_NICK="operator")
        # #425: handle_ping_request() now reserves a slot from
        # runtime.outbound_pacer before sending. It is a process-wide
        # singleton the harness does not reset between tests (see
        # tests/test_reconnect.py's identical fix for the same reason), so
        # a reservation left behind by an unrelated earlier test could make
        # this one block for up to the real MSG_DELAY (5.0s by default)
        # instead of running instantly.
        self._real_pacer = runtime.outbound_pacer
        runtime.outbound_pacer = runtime.OutboundPacer()
        self.addCleanup(setattr, runtime, "outbound_pacer", self._real_pacer)

    def test_the_probe_reaches_the_server_socket(self):
        sock = RecordingSocket()

        commands.handle_ping_request(sock, "operator", "#chan")

        self.assertIn("PING", sock.text())

    def test_it_records_who_asked_and_where(self):
        commands.handle_ping_request(RecordingSocket(), "operator", "#chan")

        self.assertEqual(config.ping_triggered_by, "operator")
        self.assertEqual(config.ping_channel_source, "#chan")
        self.assertTrue(config.ping_start_time > 0)

    def test_a_socket_failure_does_not_propagate(self):
        """An admin command must not be able to take the read loop down."""
        class Broken:
            def send(self, payload):
                raise OSError("connection reset")

        commands.handle_ping_request(Broken(), "operator", "#chan")

    def test_the_reply_reports_a_latency(self):
        commands.handle_ping_request(RecordingSocket(), "operator", "#chan")
        sent = []
        real = announce.send_debug
        announce.send_debug = lambda text, **kwargs: sent.append(text)
        self.addCleanup(setattr, announce, "send_debug", real)

        commands.handle_pong_response()

        self.assertTrue(sent, "the pong produced no reply at all")
        self.assertTrue(any("sec" in text for text in sent), sent)

    def test_a_pong_nobody_asked_for_is_ignored(self):
        """The server sends PONG for its own keepalives too."""
        config.ping_start_time = None
        sent = []
        real = announce.send_debug
        announce.send_debug = lambda text, **kwargs: sent.append(text)
        self.addCleanup(setattr, announce, "send_debug", real)

        commands.handle_pong_response()

        self.assertEqual(sent, [])


class TheFileListSend(DCCoreTestCase):
    """list.send_file_list - what answers @<nick>, the single most common
    request this bot handles."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, NICKNAME="TestBot",
                        LIST_BASE_NAME="TestBot", LIST_FORMAT="zip")

        # list.py binds oserve with a module-level `import oserve` (line 6),
        # while every other module reaches it through sys.modules.get('oserve').
        # install_fake_oserve() replaces the sys.modules entry, which does not
        # rebind an already-imported name - so the stub never reached this
        # function, and that is a large part of why nothing ever tested it.
        real = list_mod.oserve
        list_mod.oserve = self.oserve
        self.addCleanup(setattr, list_mod, "oserve", real)

    def sent(self):
        return "".join(m for _u, m, *_ in self.oserve.queued)

    def test_a_missing_list_is_reported_rather_than_ignored(self):
        list_mod.send_file_list(RecordingSocket(), "dave", "#chan")

        self.assertIn("List file missing", self.sent())

    def test_a_rebuild_in_progress_says_so_instead(self):
        """A different message on purpose: "missing" would send the operator
        looking for a problem that is about to fix itself."""
        self.set_config(update_inprogress=True)

        list_mod.send_file_list(RecordingSocket(), "dave", "#chan")
        text = self.sent()

        self.assertIn("rebuilding", text)
        self.assertNotIn("List file missing", text)

    def test_every_reply_is_a_complete_irc_command(self):
        """This function shipped a bare text line once: the server answered
        "421 Preparing :Unknown command" and the user saw nothing at all."""
        list_mod.send_file_list(RecordingSocket(), "dave", "#chan")

        for _user, message, *_rest in self.oserve.queued:
            with self.subTest(message=message[:40]):
                self.assertTrue(message.startswith(("NOTICE ", "PRIVMSG ")))
                self.assertTrue(message.endswith("\r\n"))


class TheAnnounceThreadStarter(DCCoreTestCase):
    """announce.start_announce_thread - three lines that decide whether the
    channel advert ever runs at all."""

    def test_it_starts_the_worker(self):
        """The worker itself is a `while True` and is allowlisted; what is
        checkable here is that this is what launches it."""
        import threading

        started = threading.Event()
        real = announce.announce_worker
        announce.announce_worker = started.set
        self.addCleanup(setattr, announce, "announce_worker", real)

        announce.start_announce_thread()

        self.assertTrue(started.wait(timeout=5),
                        "the advert worker was never launched")

    def test_the_thread_is_a_daemon(self):
        """A non-daemon advert timer would hold the process open at shutdown
        for up to a full advert interval."""
        import threading

        captured = {}
        real_thread = threading.Thread

        def capture(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            captured["daemon"] = kwargs.get("daemon")
            return thread

        real_worker = announce.announce_worker
        announce.announce_worker = lambda: None
        threading.Thread = capture
        self.addCleanup(setattr, threading, "Thread", real_thread)
        self.addCleanup(setattr, announce, "announce_worker", real_worker)

        announce.start_announce_thread()

        self.assertTrue(captured.get("daemon"))


class TheFirstRunWizardsEntryPoint(unittest.TestCase):
    """configure.py's main() and offer_to_generate_master_list().

    tests/test_configure.py covers the pure functions the wizard pulls out for
    exactly that purpose, and stops there - so the function that decides the
    ORDER the steps run in, and the one that decides whether a freshly
    configured install ends up with a list at all, were never entered.

    Order matters here: write_settings_conf has to land before the subprocess
    is spawned, because the whole reason that step is a subprocess is that it
    re-reads the settings this script just wrote.
    """

    def setUp(self):
        import configure
        self.setup = configure
        # The stats-import offer reads from the terminal, so every test that
        # drives main() has to insulate itself from it or block on stdin.
        # Stubbed here rather than in each test: what it does is
        # test_configure.py's subject, and what main() does with it is only
        # that it is reached, which the first test below asserts off this.
        self.import_offer = self.stub("offer_to_import_omenserve_stats")
        # main() asks settings.conf whether it can take the answers before
        # writing anything (#1272). That is tests/test_a_broken_settings_file_
        # is_not_a_first_run.py's subject; here the writers are stubs, and the
        # answers name folders that need not exist on this machine.
        self.stub("settings_conf_problem")
        import settings_file
        self.addCleanup(setattr, settings_file, "check_save", settings_file.check_save)
        settings_file.check_save = lambda *args, **kwargs: None

    def stub(self, name, result=None):
        calls = []
        real = getattr(self.setup, name)

        def recorder(*args, **kwargs):
            calls.append((args, kwargs))
            return result

        setattr(self.setup, name, recorder)
        self.addCleanup(setattr, self.setup, name, real)
        return calls

    def test_main_runs_every_step(self):
        answers = self.stub("collect_answers", ({"NICKNAME": "Bot"}, "hash"))
        settings = self.stub("write_settings_conf")
        password = self.stub("write_admin_config_password")
        listing = self.stub("offer_to_generate_master_list")

        self.setup.main()

        self.assertEqual(len(answers), 1)
        self.assertEqual(len(settings), 1)
        self.assertEqual(len(password), 1)
        self.assertEqual(len(listing), 1)
        # The OmenServe import, added after this test was written and the
        # reason it is no longer "the four steps": an offer nothing reaches
        # is not an offer.
        self.assertEqual(len(self.import_offer), 1)

    def test_the_answers_reach_the_writers(self):
        """A main() that called them all with nothing would satisfy the counts
        above."""
        self.stub("collect_answers", ({"NICKNAME": "Bot"}, "the-hash"))
        settings = self.stub("write_settings_conf")
        password = self.stub("write_admin_config_password")
        self.stub("offer_to_generate_master_list")

        self.setup.main()

        self.assertEqual(settings[0][0][0], {"NICKNAME": "Bot"})
        self.assertEqual(password[0][0][0], "the-hash")

    def test_the_list_step_is_told_whether_a_directory_was_chosen(self):
        """FILE_DIRECTORY is optional, and building a list without one is not
        a thing that can work."""
        for changes, expected in (({"FILE_DIRECTORY": "/music"}, True),
                                  ({"NICKNAME": "Bot"}, False)):
            with self.subTest(changes=changes):
                self.stub("collect_answers", (changes, "hash"))
                self.stub("write_settings_conf")
                self.stub("write_admin_config_password")
                listing = self.stub("offer_to_generate_master_list")

                self.setup.main()

                self.assertEqual(listing[0][0][0], expected)

    def test_the_settings_are_written_before_the_subprocess_runs(self):
        """The ordering the subprocess exists for: it re-reads settings.conf
        in a fresh process, so writing that file afterwards would build the
        list against the previous configuration."""
        order = []
        for name in ("write_settings_conf", "write_admin_config_password",
                     "offer_to_generate_master_list"):
            real = getattr(self.setup, name)
            self.addCleanup(setattr, self.setup, name, real)
            setattr(self.setup, name,
                    (lambda n: lambda *a, **k: order.append(n))(name))
        self.stub("collect_answers", ({"FILE_DIRECTORY": "/music"}, "hash"))

        self.setup.main()

        self.assertLess(order.index("write_settings_conf"),
                        order.index("offer_to_generate_master_list"))

    def test_no_directory_means_no_list_build(self):
        import subprocess

        calls = []
        real = subprocess.run
        subprocess.run = lambda *a, **k: calls.append(a)
        self.addCleanup(setattr, subprocess, "run", real)

        self.setup.offer_to_generate_master_list(False)

        self.assertEqual(calls, [],
                         "a list build with no library configured can only fail")

    def test_a_directory_offers_the_list_build(self):
        """Control for the test above. Answered "no" at the prompt, so the
        build itself is not spawned - what is being checked is that the
        function gets as far as asking, rather than returning immediately."""
        import builtins
        import subprocess

        asked = []
        real_input = builtins.input
        builtins.input = lambda prompt="": (asked.append(prompt), "n")[1]
        self.addCleanup(setattr, builtins, "input", real_input)

        calls = []
        real_run = subprocess.run
        subprocess.run = lambda *a, **k: calls.append(a)
        self.addCleanup(setattr, subprocess, "run", real_run)

        self.setup.offer_to_generate_master_list(True)

        self.assertTrue(asked, "the operator was never asked")


class TheWebDependenciesAreOfferedOnFirstRun(unittest.TestCase):
    """configure.py's offer_to_install_web_requirements() (#69): answering
    "yes" to enabling the dashboard used to just print `pip install flask`
    and leave it there, so the very first run of a freshly enabled
    dashboard failed with an ImportError the operator had to go find
    themselves. Now offers to run the install there and then.

    Flask is genuinely installed in this test environment - webserver.py's
    own tests need it - so "missing" is simulated with sys.modules['flask']
    = None, the standard way to make the NEXT `import flask` raise
    ImportError without touching whatever already-loaded modules elsewhere
    in the suite hold onto from their own earlier `from flask import ...`.
    Restored in tearDown either way, so no other test sees Flask as absent.
    """

    def setUp(self):
        import configure
        self.setup = configure
        self._real_flask_entry = sys.modules.get("flask")
        self._real_pip_entry = sys.modules.get("pip")
        self._real_is_windows = configure.platform_compat.IS_WINDOWS
        import builtins
        self._real_input = builtins.input
        import subprocess
        self._real_run = subprocess.run
        self.addCleanup(self._restore)

    def _restore(self):
        import builtins
        import subprocess
        if self._real_flask_entry is not None:
            sys.modules["flask"] = self._real_flask_entry
        else:
            sys.modules.pop("flask", None)
        if self._real_pip_entry is not None:
            sys.modules["pip"] = self._real_pip_entry
        else:
            sys.modules.pop("pip", None)
        self.setup.platform_compat.IS_WINDOWS = self._real_is_windows
        builtins.input = self._real_input
        subprocess.run = self._real_run

    def make_flask_missing(self):
        sys.modules["flask"] = None

    def make_pip_missing(self):
        sys.modules["pip"] = None

    def fake_input(self, *answers):
        import builtins
        answer_iter = iter(answers)
        builtins.input = lambda prompt="": next(answer_iter)

    def record_subprocess(self, returncode=0):
        import subprocess

        class _Result:
            pass

        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            result = _Result()
            result.returncode = returncode
            return result

        subprocess.run = fake_run
        return calls

    def test_flask_already_installed_asks_nothing_and_installs_nothing(self):
        """The common case, on every run after the first: no network
        access, no prompt, for an operator who already has it."""
        import builtins

        def fail_if_asked(prompt=""):
            raise AssertionError("must not prompt when Flask is already importable")

        builtins.input = fail_if_asked
        calls = self.record_subprocess()

        self.setup.offer_to_install_web_requirements()

        self.assertEqual(calls, [])

    def test_flask_missing_and_the_operator_says_yes_installs_it(self):
        self.make_flask_missing()
        self.fake_input("y")
        calls = self.record_subprocess()

        self.setup.offer_to_install_web_requirements()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], sys.executable)
        self.assertIn("requirements-web.txt", calls[0][0][-1])

    def test_a_blank_answer_defaults_to_installing(self):
        """[Y/n] - the operator just asked to enable the dashboard, so the
        default has to be the thing that makes it work, not the thing that
        leaves it broken."""
        self.make_flask_missing()
        self.fake_input("")
        calls = self.record_subprocess()

        self.setup.offer_to_install_web_requirements()

        self.assertEqual(len(calls), 1)

    def test_flask_missing_and_the_operator_declines_does_not_install(self):
        self.make_flask_missing()
        self.fake_input("n")
        calls = self.record_subprocess()

        self.setup.offer_to_install_web_requirements()

        self.assertEqual(calls, [])

    def test_a_failed_install_does_not_raise(self):
        """Nothing here is fatal - a dashboard that cannot import Flask
        already logs it and stays off; this must not make setup itself
        crash on top of that."""
        self.make_flask_missing()
        self.fake_input("y")
        self.record_subprocess(returncode=1)

        self.setup.offer_to_install_web_requirements()  # must not raise

    def test_pip_missing_asks_nothing_and_installs_nothing(self):
        """Asking "Install it now?" when pip cannot possibly do it would be
        asking about something already known to fail - checked and refused
        before the prompt, not discovered by running a subprocess that was
        never going to work."""
        import builtins

        def fail_if_asked(prompt=""):
            raise AssertionError("must not prompt when pip is not importable")

        self.make_flask_missing()
        self.make_pip_missing()
        builtins.input = fail_if_asked
        calls = self.record_subprocess()

        self.setup.offer_to_install_web_requirements()

        self.assertEqual(calls, [])

    def test_pip_missing_on_windows_points_at_windows_md(self):
        import builtins

        def fail_if_asked(prompt=""):
            raise AssertionError("must not prompt when pip is not importable")

        self.make_flask_missing()
        self.make_pip_missing()
        self.setup.platform_compat.IS_WINDOWS = True
        builtins.input = fail_if_asked
        buf = io.StringIO()

        with contextlib.redirect_stdout(buf):
            self.setup.offer_to_install_web_requirements()

        self.assertIn("docs/WINDOWS.md", buf.getvalue())
        self.assertNotIn("docs/INSTALL.md", buf.getvalue())

    def test_pip_missing_elsewhere_points_at_install_md(self):
        """Not Windows - the "tick the py launcher/pip boxes" fix in
        WINDOWS.md does not apply, so this must not send a Linux operator
        looking for a Windows installer checkbox that does not exist for
        them."""
        import builtins

        def fail_if_asked(prompt=""):
            raise AssertionError("must not prompt when pip is not importable")

        self.make_flask_missing()
        self.make_pip_missing()
        self.setup.platform_compat.IS_WINDOWS = False
        builtins.input = fail_if_asked
        buf = io.StringIO()

        with contextlib.redirect_stdout(buf):
            self.setup.offer_to_install_web_requirements()

        self.assertIn("docs/INSTALL.md", buf.getvalue())
        self.assertNotIn("docs/WINDOWS.md", buf.getvalue())


class TheRehashHandlerIsEntered(DCCoreTestCase):
    """commands.handle_rehash_request - the most dangerous operation in the
    daemon, and one the audit found had no behavioural coverage at all.

    Covered more fully in tests/test_rehash_reloads_admin_config.py. This is
    the floor: the reload step is stubbed out, because reloading the real
    modules mid-suite resets state the rest of the run depends on.
    """

    def setUp(self):
        super().setUp()
        self.calls = []

        # importlib.reload is stubbed rather than the handler's own reload
        # step, so this holds whether or not that step has been extracted into
        # a named function yet. Reloading the real modules mid-suite would
        # reset module-level state the rest of the run is built on.
        import importlib

        real = importlib.reload
        importlib.reload = lambda module: (self.calls.append(module.__name__),
                                           module)[1]
        self.addCleanup(setattr, importlib, "reload", real)

    def test_an_authorised_rehash_reaches_the_reload(self):
        commands.handle_rehash_request("operator", "#chan", authorised=True)

        self.assertTrue(self.calls, "no module was reloaded at all")
        self.assertIn("defaults", self.calls)

    def test_an_unauthorised_rehash_does_not(self):
        self.set_config(ADMIN_NICK="operator")

        commands.handle_rehash_request("stranger", "#chan")

        self.assertEqual(self.calls, [])


# ---------------------------------------------------------------------------
# scripts/function_coverage.py itself (#1379)
#
# The measure everything above is paid against. It failed in preflight with no
# reason given: a decorated function never matched what the profiler recorded,
# and the report that would have said so went into a StringIO a test had left
# in sys.stdout. These drive the script's own functions on small temp modules.
# ---------------------------------------------------------------------------

def load_function_coverage():
    """A private copy of scripts/function_coverage.py. Never put in
    sys.modules, so patching its REPO_ROOT, SRC, ALLOWLIST or run_suite below
    changes nothing for anything else in the run."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "function_coverage_under_test",
        os.path.join(REPO_ROOT, "scripts", "function_coverage.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_from(path, name):
    """The module at `path`, executed for real, as Python itself compiles it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def line_of(text, fragment):
    """1-based number of the first line of `text` that starts with `fragment`."""
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(fragment):
            return number
    raise AssertionError(f"{fragment!r} is not in the sample")


class CoverageScriptCase(unittest.TestCase):
    """run_suite() installs its own profiler, on sys and on threading, and
    puts back whatever profiler was there before - function_coverage.py's
    own, when this file runs inside its pass. Each test here still starts
    with none and puts back whatever it found, so a run_suite() that fails
    to restore cannot end the outer pass's recording; and it puts the
    streams back, so one that fails to restore those cannot swallow the rest
    of this process's output."""

    def setUp(self):
        import threading

        # scripts/function_coverage.py is dev-only tooling, stripped at
        # extraction (docs/PUBLIC-REPO-WORKFLOW.md, step 2): there is nothing
        # to test in the public tree (#1409).
        if _this_is_an_export(self):
            self.skipTest("scripts/function_coverage.py is stripped from an extracted "
                          "public tree; it is tested in the development repository")
        self.fc = load_function_coverage()
        self.root = temp_dir(self, prefix="dccore-coverage-")
        self.addCleanup(setattr, sys, "stderr", sys.stderr)
        self.addCleanup(setattr, sys, "stdout", sys.stdout)
        self.addCleanup(threading.setprofile, threading.getprofile())
        self.addCleanup(sys.setprofile, sys.getprofile())
        sys.setprofile(None)
        threading.setprofile(None)

    def assert_no_profiler_left(self):
        import threading

        self.assertIsNone(sys.getprofile(), "run_suite() left its profiler on")
        self.assertIsNone(threading.getprofile(),
                          "run_suite() left its profiler set for new threads")


# Written into temp directories at run time: names being CREATED, so they are
# named here once rather than as literals in the path calls, where
# tests/test_referenced_files_exist.py would look for them in the tree.
KEYS_MODULE = "probe_keys.py"
ENTRY_MODULE = "probe_entry.py"


# Lines matter here: each function's key is checked against the first line
# Python itself gives it. `several` has its first decorator three lines above
# its last; `parenthesised` uses the relaxed decorator grammar (3.9+), whose
# expression starts a line below its `@`.
KEYING_SAMPLE = '''\
def _same(function):
    return function


def _tag(*_labels):
    return _same


def plain():
    return "plain"


@_same
def single():
    return "single"


@_tag(
    "first")
@_tag("second")
@_same
def several():
    return "several"


@_same
async def later():
    return "later"


@(
    _same)
def parenthesised():
    return "parenthesised"
'''


class TheCoverageKeyIsTheLineTheProfilerRecords(CoverageScriptCase):
    """public_functions() against co_firstlineno, which is what Recorder
    stores for every call. For a decorated function that is the line of its
    first decorator, not its def line - and keyed on the def line,
    list.py::list_dirs_held read as never entered although tests call it."""

    def keys_and_truth(self):
        src = os.path.join(self.root, "src")
        os.mkdir(src)
        path = write(os.path.join(src, KEYS_MODULE), KEYING_SAMPLE)
        self.fc.REPO_ROOT = self.root   # holds no .py file of its own
        self.fc.SRC = src

        self.compiled = load_from(path, "probe_keys_under_test")
        truth = {(KEYS_MODULE, name): getattr(self.compiled, name).__code__.co_firstlineno
                 for name in ("plain", "single", "several", "later", "parenthesised")}
        return self.fc.public_functions(), truth

    def test_every_key_is_what_python_gives_the_function(self):
        keys, truth = self.keys_and_truth()

        self.assertEqual(keys, truth)

    def test_the_sample_tells_def_lines_from_first_lines(self):
        """The test above means something only if the def line and the first
        line differ for the decorated ones - and for `several`, if its first
        and last decorators do."""
        import inspect

        keys, truth = self.keys_and_truth()

        self.assertEqual(truth[(KEYS_MODULE, "plain")],
                         line_of(KEYING_SAMPLE, "def plain"))
        for name in ("single", "several", "later", "parenthesised"):
            with self.subTest(name=name):
                def_line = line_of(KEYING_SAMPLE, ("def " + name, "async def " + name))
                self.assertLess(truth[(KEYS_MODULE, name)], def_line)
                self.assertEqual(keys[(KEYS_MODULE, name)],
                                 truth[(KEYS_MODULE, name)])
        self.assertEqual(keys[(KEYS_MODULE, "several")],
                         line_of(KEYING_SAMPLE, "@_tag("))
        self.assertTrue(inspect.iscoroutinefunction(self.compiled.later))


ENTRY_SAMPLE = '''\
def _same(function):
    return function


@_same
def entered_through_a_decorator():
    return "called"


@_same
@_same
def never_called():
    return "not called"
'''


class ADecoratedFunctionCalledByATestCountsAsEntered(CoverageScriptCase):
    """main() end to end: public_functions() over a temp tree, the real
    run_suite() over a one-test suite that calls a decorated function, and
    main()'s own matching on (basename, name, line). main() discovers tests/
    itself, so its run_suite is pointed at that suite instead.

    The allowlist names the function nothing calls. main() exits 0 only if
    the called one matched what the profiler recorded AND the uncalled one
    did not - otherwise it reports a new uncovered function, or an allowed
    one that is now covered."""

    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())   # main() moves to REPO_ROOT
        saved_path = list(sys.path)               # ...and puts it on sys.path
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), saved_path))

        os.mkdir(os.path.join(self.root, "src"))
        path = write(os.path.join(self.root, ENTRY_MODULE), ENTRY_SAMPLE)
        self.fc.REPO_ROOT = self.root
        self.fc.SRC = os.path.join(self.root, "src")
        self.fc.ALLOWLIST = write(os.path.join(self.root, "allowed.txt"),
                                  f"{ENTRY_MODULE}::never_called  # on purpose\n")

        probe = load_from(path, "probe_entry_under_test")
        suite = unittest.TestSuite(
            [unittest.FunctionTestCase(probe.entered_through_a_decorator)])
        real_run_suite = self.fc.run_suite
        self.fc.run_suite = lambda: real_run_suite(suite=suite)

    def test_main_finds_it_entered(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.fc.main()

        report = out.getvalue()
        self.assertIn("Public daemon functions: 2", report)
        self.assertIn("Never entered by any test: 1", report)
        self.assertIn("No public daemon function is uncovered outside the allowlist.",
                      report)
        self.assertEqual(code, 0, report)


class TheCoverageRunPutsTheStreamsBack(CoverageScriptCase):
    """run_suite() prints nothing itself; main() prints the report after it.
    A test that leaves sys.stdout replaced used to take that report with it."""

    def run_one(self, function):
        return self.fc.run_suite(suite=unittest.TestSuite(
            [unittest.FunctionTestCase(function)]))

    def test_a_test_that_keeps_stdout_does_not_keep_it_past_the_run(self):
        before = sys.stdout

        def keeps_stdout():
            sys.stdout = io.StringIO()

        _seen, result = self.run_one(keeps_stdout)

        self.assertEqual((result.testsRun, result.wasSuccessful()), (1, True))
        self.assertIs(sys.stdout, before)
        self.assert_no_profiler_left()

    def test_a_test_that_keeps_stderr_does_not_keep_it_past_the_run(self):
        before = sys.stderr

        def keeps_stderr():
            sys.stderr = io.StringIO()

        _seen, result = self.run_one(keeps_stderr)

        self.assertEqual((result.testsRun, result.wasSuccessful()), (1, True))
        self.assertIs(sys.stderr, before)
        self.assert_no_profiler_left()

    def test_a_run_that_raises_still_puts_both_back_and_the_profiler_off(self):
        before = (sys.stdout, sys.stderr)

        class Explodes:
            """A suite whose run takes both streams and then raises."""

            def __call__(self, result):
                sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
                raise RuntimeError("the suite itself broke (test)")

            def countTestCases(self):
                return 1

        with self.assertRaises(RuntimeError):
            self.fc.run_suite(suite=Explodes())

        self.assertIs(sys.stdout, before[0])
        self.assertIs(sys.stderr, before[1])
        self.assert_no_profiler_left()

    def test_the_profiler_saw_the_suite(self):
        """The streams are put back without taking the recording with them."""
        calls = []

        def a_function_the_suite_calls():
            calls.append(True)

        seen, _result = self.run_one(a_function_the_suite_calls)

        code = a_function_the_suite_calls.__code__
        self.assertEqual(calls, [True])
        self.assertIn((code.co_filename, code.co_name, code.co_firstlineno), seen)


class TheCoverageRunPutsAnOuterProfilerBack(CoverageScriptCase):
    """run_suite() is itself called by tests that run while
    function_coverage.py profiles the suite. Its Recorder used to stop with
    setprofile(None), which ended that outer recording for the rest of the
    pass: everything after the test read as never entered."""

    def install_outer_profiler(self):
        import threading

        def outer(frame, event, arg):
            pass

        sys.setprofile(outer)
        threading.setprofile(outer)
        return outer

    def assert_outer_profiler_back(self, outer):
        import threading

        self.assertIs(sys.getprofile(), outer,
                      "run_suite() did not put back the profiler it found")
        self.assertIs(threading.getprofile(), outer,
                      "run_suite() did not put back the profiler for new threads")

    def test_a_run_puts_back_the_profiler_it_found(self):
        outer = self.install_outer_profiler()

        _seen, result = self.fc.run_suite(suite=unittest.TestSuite(
            [unittest.FunctionTestCase(lambda: None)]))

        self.assertEqual((result.testsRun, result.wasSuccessful()), (1, True))
        self.assert_outer_profiler_back(outer)

    def test_a_run_that_raises_puts_it_back_too(self):
        outer = self.install_outer_profiler()

        class Explodes:
            def __call__(self, result):
                raise RuntimeError("the suite itself broke (test)")

            def countTestCases(self):
                return 1

        with self.assertRaises(RuntimeError):
            self.fc.run_suite(suite=Explodes())

        self.assert_outer_profiler_back(outer)


if __name__ == "__main__":
    unittest.main()
