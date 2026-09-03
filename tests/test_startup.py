"""oserve.py's boot sequence - the one path CI could never execute.

Every module was imported and every unit tested, but the boot itself only ran
when somebody started the real bot, which connects to Undernet and joins live
channels. It is also the first thing a Windows port meets, so these run on both
platforms in CI.

Nothing here touches the network: run_forever() is the only thing that calls
irc.irc_loop(), and it is always given a stub.
"""

import importlib
import io
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import db  # noqa: E402
import irc  # noqa: E402
import queue_mgr  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def real_oserve():
    """The actual module, not tests.support's stub.

    support.install_fake_oserve() replaces sys.modules["oserve"] so the rest of
    the suite can run single-threaded. These tests need the real thing, and test
    order is not guaranteed, so it is re-imported rather than assumed.
    """
    sys.modules.pop("oserve", None)
    return importlib.import_module("oserve")


class BootCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.FILE_DIRECTORY = self.tree.music
        config.LOCAL_LIST_DIR = self.tree.lists
        config.BANS_FILE = os.path.join(self.tree.root, "bans.txt")
        config.DCC_QUEUE_FILE = os.path.join(self.tree.root, "dcc_queue.txt")

        # #170's RFC: oserve.startup() now refuses to boot while any of
        # settings_file.REQUIRED still resolves to its shipped default (or is
        # blank) - see settings_file.unconfigured_required(). This models an
        # already-configured install; RequiredSettingsGateTests below is what
        # actually exercises the gate itself, against values left at their
        # shipped defaults on purpose. set_config() restores every one of
        # these after each test, so nothing here leaks into a test that runs
        # afterwards and never touches them itself.
        self.set_config(
            NICKNAME="TestBot",
            SERVER="irc.test.example",
            CHANNEL="#test-channel",
            ADMIN_NICK="TestAdmin",
            DEBUG_CHANNEL="#test-debug",
        )

        self.oserve = real_oserve()

        # startup() spawns the flood-queue worker. Stubbed so the suite does not
        # accumulate a live worker thread per test.
        self.workers = []
        self._real_worker = queue_mgr.queue_worker
        queue_mgr.queue_worker = lambda: self.workers.append(1)
        self.addCleanup(lambda: setattr(queue_mgr, "queue_worker", self._real_worker))

    def boot(self):
        """Run startup(), capturing its console output."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.oserve.startup()
        return buffer.getvalue()


class StartupRunsOnThisPlatform(BootCase):
    """The smoke test: the whole boot, on whichever OS is running it."""

    def test_a_clean_boot_succeeds(self):
        output = self.boot()
        self.assertIn(config.SCRIPT_VERSION, output)

    def test_it_starts_exactly_one_queue_worker(self):
        """The comment in oserve calls this out: one [QUEUE] line at boot."""
        self.boot()
        # The thread is real even though the target is stubbed; give it a moment.
        for _ in range(200):
            if self.workers:
                break
            threading.Event().wait(0.01)
        self.assertEqual(len(self.workers), 1)

    def test_a_missing_music_directory_stops_the_daemon(self):
        """sys.exit(1) - unchanged from before the split. A SET but wrong
        FILE_DIRECTORY is a real misconfiguration, unlike simply not having
        chosen one yet (see the next test)."""
        config.FILE_DIRECTORY = os.path.join(self.tree.root, "not-there")
        with self.assertRaises(SystemExit) as caught:
            self.boot()
        self.assertEqual(caught.exception.code, 1)

    def test_a_blank_music_directory_warns_but_still_boots(self):
        """FILE_DIRECTORY is deliberately NOT in settings_file.REQUIRED (see
        its own comment) - found live, running configure.py against a real
        install: requiring it blocked the daemon from ever reaching the web
        dashboard, the one place that is genuinely easier to set it from.
        Blank must not raise (os.path.exists(None) does) and must not exit -
        only warn."""
        self.set_config(FILE_DIRECTORY=None)
        output = self.boot()  # must not raise
        self.assertIn("[WARNING] No music directory configured yet", output)
        self.assertIn(config.SCRIPT_VERSION, output)

    def test_it_warns_but_continues_with_no_master_list(self):
        """A fresh install has no list yet; that must not stop the boot."""
        output = self.boot()
        self.assertIn("No file list found", output)

    def test_a_required_setting_still_at_its_shipped_default_stops_the_daemon(self):
        """#170's RFC, the daemon's own hard backstop. BootCase's own setUp()
        already overrode every settings_file.REQUIRED name away from its
        shipped default - undoing just NICKNAME here is what a bot that
        never got configured at all looks like, minus the other five."""
        self.set_config(NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"])
        with self.assertRaises(SystemExit) as caught:
            self.boot()
        self.assertEqual(caught.exception.code, 1)

    def test_the_refusal_names_which_settings_are_still_unconfigured(self):
        self.set_config(
            NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"],
            CHANNEL=config.SHIPPED_DEFAULTS["CHANNEL"],
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(SystemExit):
                self.oserve.startup()
        output = buffer.getvalue()
        self.assertIn("NICKNAME", output)
        self.assertIn("CHANNEL", output)
        # The four this test left alone must not be reported alongside them.
        self.assertNotIn("SERVER", output)
        self.assertNotIn("ADMIN_NICK", output)

    def test_a_blank_required_setting_also_stops_the_daemon(self):
        """The other half of "unconfigured": a fresh install that uncommented
        a REQUIRED line in settings.conf.sample but left it blank, rather
        than one that never touched it at all."""
        self.set_config(CHANNEL="")
        with self.assertRaises(SystemExit) as caught:
            self.boot()
        self.assertEqual(caught.exception.code, 1)

    def test_a_value_that_merely_resembles_the_upstream_brand_is_not_flagged(self):
        """The gate only ever checks blank-vs-not (NICKNAME's shipped
        default is None, not a real value - see config.py's own comment on
        why). A nickname that happens to still contain "DCCore" - like this
        install's own real "DCCoreWeb" - is a real, deliberate operator
        choice, not an untouched default, so it is never flagged."""
        self.set_config(NICKNAME="DCCoreWeb")
        output = self.boot()  # must not raise
        self.assertIn(config.SCRIPT_VERSION, output)

    def test_it_reports_the_list_when_there_is_one(self):
        name = f"{config.LIST_BASE_NAME}-2026-08-26.txt"
        with open(os.path.join(self.tree.lists, name), "w", encoding="utf-8") as handle:
            handle.write("List of 0 Files\n")
        output = self.boot()
        self.assertIn(name, output)

    def test_it_loads_saved_bans(self):
        config.banned_users = {"dave": 9999999999.0}
        db.save_bans_to_file()
        config.banned_users = {}
        self.boot()
        self.assertIn("dave", config.banned_users)

    def test_it_loads_the_saved_queue(self):
        loaded = []
        real = db.load_dcc_queue
        db.load_dcc_queue = lambda: loaded.append(1)
        self.addCleanup(lambda: setattr(db, "load_dcc_queue", real))
        self.boot()
        self.assertEqual(len(loaded), 1, "a queue saved before a restart must come back")

    def test_it_loads_saved_fetch_history(self):
        """A finished fetch's row (the only thing the dashboard Downloads
        table and its Delete button have to point at) must survive a
        restart the same way the bot registry and fetched-lists registry
        already do - see db.load_fetch_history()'s own docstring."""
        loaded = []
        real = db.load_fetch_history
        db.load_fetch_history = lambda: (loaded.append(1), {})[-1]
        self.addCleanup(lambda: setattr(db, "load_fetch_history", real))
        self.boot()
        self.assertEqual(len(loaded), 1, "finished fetches saved before a restart must come back")

    def test_no_bans_file_is_not_an_error(self):
        self.assertFalse(os.path.exists(config.BANS_FILE))
        self.boot()

    def test_a_missing_bans_file_says_so(self):
        """The gap this guards: a wrong working directory makes BANS_FILE's
        relative path resolve to nowhere, and the daemon used to start with
        an empty ban list and no way to tell that apart from every ban
        having already expired."""
        self.assertFalse(os.path.exists(config.BANS_FILE))
        output = self.boot()
        self.assertIn(config.BANS_FILE, output)
        self.assertIn("no active bans", output.lower())


class ReconnectLoopResetsTheGlobals(BootCase):
    """The trap this refactor had to avoid.

    irc_connection and bot_joined_channel used to be assigned at MODULE level
    inside __main__, so they rebound oserve.irc_connection and
    oserve.bot_joined_channel - which irc.py and dcc.py reach through
    sys.modules to find the live socket. Moved into a function without a global
    declaration they become locals, the reconnect cleanup silently stops
    happening, and nothing reports it.
    """

    class BreakOut(Exception):
        """Ends the otherwise infinite loop after one pass."""

    def run_one_pass(self, loop_raises):
        self.oserve.irc_connection = "a stale socket"
        self.oserve.bot_joined_channel = True

        real_loop, real_sleep = irc.irc_loop, self.oserve.time.sleep

        def stub_loop():
            raise loop_raises

        def stub_sleep(_seconds):
            raise self.BreakOut()

        irc.irc_loop = stub_loop
        self.oserve.time.sleep = stub_sleep
        self.addCleanup(lambda: setattr(irc, "irc_loop", real_loop))
        self.addCleanup(lambda: setattr(self.oserve.time, "sleep", real_sleep))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            try:
                self.oserve.run_forever()
            except self.BreakOut:
                pass
        return buffer.getvalue()

    def test_a_dropped_connection_clears_the_socket_reference(self):
        self.run_one_pass(RuntimeError("network died"))
        self.assertIsNone(self.oserve.irc_connection,
                          "a stale socket left here is handed to the next connection")

    def test_a_dropped_connection_clears_the_joined_flag(self):
        self.run_one_pass(RuntimeError("network died"))
        self.assertFalse(self.oserve.bot_joined_channel)

    def test_the_advert_is_told_the_connection_is_gone(self):
        import announce
        announce.is_ready = True
        self.run_one_pass(RuntimeError("network died"))
        self.assertFalse(announce.is_ready)

    def test_a_crash_in_the_loop_is_reported_not_swallowed(self):
        output = self.run_one_pass(RuntimeError("network died"))
        self.assertIn("CRITICAL MAIN ERROR", output)
        self.assertIn("network died", output)

    def test_ctrl_c_exits_cleanly(self):
        real_loop = irc.irc_loop

        def stub_loop():
            raise KeyboardInterrupt()

        irc.irc_loop = stub_loop
        self.addCleanup(lambda: setattr(irc, "irc_loop", real_loop))

        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                self.oserve.run_forever()
        self.assertEqual(caught.exception.code, 0)


class TheEntryPointStillWiresItUp(unittest.TestCase):
    """Splitting the block is only safe if __main__ still calls both halves."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_main_calls_startup_then_run_forever(self):
        tail = self.source.split('if __name__ == "__main__":', 1)[1]
        self.assertIn("startup()", tail)
        self.assertIn("run_forever()", tail)
        self.assertLess(tail.index("startup()"), tail.index("run_forever()"),
                        "the boot must happen before the network loop")

    def test_the_reconnect_loop_declares_its_globals(self):
        """Belt and braces alongside the behavioural tests above."""
        body = self.source.split("def run_forever", 1)[1]
        self.assertIn("global irc_connection, bot_joined_channel", body)


class TheCrossBotFetchDispatcherIsStarted(BootCase):
    """The other background worker startup() launches, and until now the only
    one nothing checked.

    queue_mgr.queue_worker has had test_it_starts_exactly_one_queue_worker
    since this file was written. dcc_fetch.fetch_dispatcher_worker sits eleven
    lines below it in oserve.py, does the same kind of job, and had nothing.

    The asymmetry matters because of what the worker does. It is the only thing
    that ever calls check_fetch_queue(), which is what moves a fetch from
    "pending" to "offered" to "receiving". Delete the thread and every function
    it drives still passes its own tests - check_fetch_queue() has plenty - and
    the daemon still boots, still answers the dashboard, still serves files.
    Cross-bot fetches simply sit at "pending" forever, and nothing anywhere
    says why.

    That is the shape of #119: a correct, well-tested function that no live
    path reached, so the speed record read zero for the life of the daemon.
    """

    def setUp(self):
        super().setUp()
        import dcc_fetch
        self.dcc_fetch = dcc_fetch
        # Stubbed like the queue worker above, and for the same reason: the
        # real one is a while True loop, and the suite would accumulate one
        # live thread per test that boots.
        self.dispatchers = []
        self._real_dispatcher = dcc_fetch.fetch_dispatcher_worker
        dcc_fetch.fetch_dispatcher_worker = lambda: self.dispatchers.append(1)
        self.addCleanup(setattr, dcc_fetch, "fetch_dispatcher_worker",
                        self._real_dispatcher)

    def _wait_for_dispatchers(self):
        """The thread is real even though its target is stubbed."""
        for _ in range(200):
            if self.dispatchers:
                break
            threading.Event().wait(0.01)
        return self.dispatchers

    def test_it_starts_exactly_one_dispatcher(self):
        self.boot()

        self.assertEqual(
            len(self._wait_for_dispatchers()), 1,
            "nothing is driving check_fetch_queue(), so a cross-bot fetch "
            "would be accepted and then sit at 'pending' for ever")

    def test_it_starts_alongside_the_queue_worker_not_instead_of_it(self):
        """They are deliberately separate loops - queue_mgr paces outbound
        socket writes one at a time and is already dense; this one only touches
        config.fetch_queue and never blocks on the network. A refactor that
        folded one into the other would show up here."""
        self.boot()
        self._wait_for_dispatchers()

        for _ in range(200):
            if self.workers:
                break
            threading.Event().wait(0.01)

        self.assertEqual(len(self.workers), 1)
        self.assertEqual(len(self.dispatchers), 1)

    def test_a_dispatcher_that_cannot_be_started_does_not_stop_the_daemon(self):
        """oserve wraps the import and the start in try/except on purpose.
        Cross-bot fetch is an optional feature; serving files is not, and a
        daemon that refuses to boot because an optional worker could not start
        is the worse of the two failures.

        Broken at the IMPORT, which is where the guard actually is. Raising
        inside the worker instead would prove nothing: that happens on the new
        thread, after start() has already returned, so the try/except in
        startup() never sees it.
        """
        self.addCleanup(sys.modules.__setitem__, "dcc_fetch", self.dcc_fetch)
        sys.modules["dcc_fetch"] = None      # makes `import dcc_fetch` raise

        output = self.boot()

        self.assertIn(config.SCRIPT_VERSION, output,
                      "the daemon did not finish booting")
        self.assertIn("Could not start fetch dispatcher", output,
                      "the failure was swallowed with nothing said - an "
                      "operator would see fetches hang and have no reason why")


if __name__ == "__main__":
    unittest.main()
