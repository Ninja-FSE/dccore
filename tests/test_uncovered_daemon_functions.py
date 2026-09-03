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
import update_list  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402


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
    ORDER those four run in, and the one that decides whether a freshly
    configured install ends up with a list at all, were never entered.

    Order matters here: write_settings_conf has to land before the subprocess
    is spawned, because the whole reason that step is a subprocess is that it
    re-reads the settings this script just wrote.
    """

    def setUp(self):
        import configure
        self.setup = configure

    def stub(self, name, result=None):
        calls = []
        real = getattr(self.setup, name)

        def recorder(*args, **kwargs):
            calls.append((args, kwargs))
            return result

        setattr(self.setup, name, recorder)
        self.addCleanup(setattr, self.setup, name, real)
        return calls

    def test_main_runs_the_four_steps(self):
        answers = self.stub("collect_answers", ({"NICKNAME": "Bot"}, "hash"))
        settings = self.stub("write_settings_conf")
        password = self.stub("write_admin_config_password")
        listing = self.stub("offer_to_generate_master_list")

        self.setup.main()

        self.assertEqual(len(answers), 1)
        self.assertEqual(len(settings), 1)
        self.assertEqual(len(password), 1)
        self.assertEqual(len(listing), 1)

    def test_the_answers_reach_the_writers(self):
        """A main() that called all four with nothing would satisfy the counts
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


if __name__ == "__main__":
    unittest.main()
