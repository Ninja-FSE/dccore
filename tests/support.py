"""Shared scaffolding for the DCCore test suite.

The daemon keeps all of its live state in ``config`` as module globals, and its
modules reach for each other through ``sys.modules`` at call time rather than
through imports. That is fine for a long-running process and awkward for tests,
so everything needed to put the modules into a known state lives here.

Deliberately stdlib-only. The daemon itself has no dependencies and runs in a
minimal LXC, so the tests must run there too - and on Windows, where the port is
headed - with nothing more than a Python install.
"""

import os
import shutil
import sys
import tempfile
import threading
import types
import unittest

# Import the daemon's modules from the repository root, one level up.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import runtime  # noqa: E402
import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402


# Pristine references, captured once at import. The helpers below replace functions
# on shared modules, and sys.modules is shared across every test module in a run -
# so without restoring them, whichever module ran first would silently disable disk
# writes or debug output for everything after it. That is not hypothetical: it made
# the persistence tests pass alone and fail in the full suite.
_PRISTINE = [
    (db, "save_dcc_queue", db.save_dcc_queue),
    (db, "save_bans_to_file", db.save_bans_to_file),
    (db, "save_advanced_stats", db.save_advanced_stats),
    (db, "save_speed_record", db.save_speed_record),
    (announce, "send_debug", announce.send_debug),
    (announce, "send_dcc_sending_notice", announce.send_dcc_sending_notice),
    (announce, "send_transfer_complete", announce.send_transfer_complete),
    (announce, "send_dcc_error", announce.send_dcc_error),
    (announce, "send_dcc_queue_notice", announce.send_dcc_queue_notice),
    (announce, "send_pack_error_notice", announce.send_pack_error_notice),
    (dcc, "start_dcc_send", dcc.start_dcc_send),
    (dcc, "check_queue_and_send", dcc.check_queue_and_send),
]


def restore_daemon_functions():
    """Undo every stub any test installed on a shared module."""
    for module, name, original in _PRISTINE:
        setattr(module, name, original)


# Names config.py assigns in its "GLOBALT LIVE-MINNE" section, plus the ones other
# modules attach at runtime. Every one of these has to be reset between tests or a
# case inherits whatever the previous one left behind.
RUNTIME_CONTAINERS = {
    "dcc_queue": dict,
    "active_transfers": list,
    "banned_users": dict,
    "frozen_queues": dict,
    "channel_users": dict,
    "user_requests": dict,
    "muted_until": dict,
    "whois_status": dict,
    "failed_transfers": dict,
    "vip_queue": list,
    "send_queue": dict,
    "user_processing_lock": set,
    "broadcast_search_results": list,
    "fetch_queue": dict,
    "fetched_bot_lists": dict,
    # Added late, and the reason is worth keeping: this was the one container
    # runtime.py exposes that nothing here reset. It cost nothing while only
    # irc.py read it, and became three failures the day a dashboard view
    # started reading it too - all of them passing alone and failing in the
    # full run. test_runtime_state.py now derives the comparison rather than
    # leaving the next one to be found the same way.
    "known_bots": dict,
    # Offers in flight. A leftover here is not inert: it is keyed by (nick,
    # port), the DCC port range is small and reused, and a stale entry would
    # hand the next test's send an offset agreed for a different file.
    "dcc_send_offers": dict,
    # FAIL and SEARCH counts since the process started (#754). Cleared between
    # tests so one test's failures are not the next one's; the code that reads
    # it treats a missing key as zero.
    "feed_counts": dict,
    # Channels we have been kicked from, and how many rejoins have been
    # refused. Left behind, a test that provokes a kick makes the next one
    # think it is banned from a channel it never left - and the advert worker
    # would try to rejoin it.
    "kicked_channels": dict,
    # Operator notices. A leftover here is a badge in the next test
    # counting an event from the last one.
    "notices": list,
    "notice_state": dict,
    # #376's alt-nick merge. A leftover departure or alias here is exactly
    # the false positive the feature's own safeguards exist to avoid - a
    # later, unrelated test's nick could match a timestamp this test left
    # behind and get merged with it in the List Browser.
    "recent_departures": dict,
    "nick_aliases": dict,
    # Same reasoning: a leftover is one test's message showing up in the next
    # test's panel.
    "private_messages": list,
    "private_message_state": dict,
    # And the burst window, or one test's flood ceiling is still half full
    # when the next test asks whether a reply went out.
    "private_message_decline_sends": list,
}

# SETTINGS A TEST MAY CHANGE AND MUST NOT LEAVE CHANGED.
#
# Distinct from RUNTIME_FLAGS below, which is live state. These are ordinary
# config values with a module-level default - and the trouble with those is
# that a test which sets one is usually testing something else entirely, so
# nothing about it looks like state management.
#
# BROADCAST_SEARCH_CHANNEL is the one that proved it. tests/
# test_config_overrides.py sets it while checking that settings.conf overrides
# a module default - which is exactly what that file is for - and nothing put
# it back. Every later test then saw a channel it never configured.
#
# What that cost: test_a_bot_nowhere_we_know_of_falls_back_to_the_first_channel
# reads the fallback channel through the same value, so it failed with
# "'PRIVMSG #one :' not found in 'PRIVMSG #dccore-test :...'" - naming a
# channel from a test file it has nothing to do with, in a run where three
# consecutive full suites had just passed. It was read as a flake twice before
# it was read as a leak.
SETTINGS_DEFAULTS = {
    "BROADCAST_SEARCH_CHANNEL": None,
    # A test that turns private messages off, or names an admin, must not
    # leave either behind: the next test would be talking to strangers.
    "PRIVATE_MESSAGES_ENABLED": True,
    "PRIVATE_MESSAGE_DECLINE_TEXT": (
        "This bot does not accept private messages. "
        "Please message %admin instead."),
    "PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS": 86400,
    "PRIVATE_MESSAGE_DECLINE_BURST": 20,
    "PRIVATE_MESSAGE_DECLINE_BURST_SECONDS": 600,
    "ADMIN_NICK": None,
    # The channel admin commands check the host once this is set (#579), so a
    # test that set it and did not put it back - two do - changed the answer of
    # every is_admin() call after it.
    "ADMIN_HOSTMASKS": [],
    # The outbound pace (#667). Six setUps set these to 10-50 ms directly
    # and nothing put the shipped 5.0 s / 0 back, so every test after them
    # in the run - alphabetically most of the suite - was paced at 10 ms
    # and would stall or time out run on its own. They are set through
    # set_config() now, and the shipped values return here on every reset;
    # a guard reads defaults.py to keep these two the shipped ones.
    "MSG_DELAY": 5.0,
    "DEBUG_MSG_DELAY": 0.0,
}

RUNTIME_FLAGS = {
    "search_inprogress": False,
    "rar_inprogress": False,
    "bot_joined_channel": True,
    "activation_triggered": False,
    "update_inprogress": False,
    "last_list_update_ok": None,
    "last_list_update_error": None,
    # A leftover here is worse than a missing one: it would be shown against
    # a rebuild it did not measure.
    "last_list_update_seconds": None,
    "connection_epoch": 1,
    "broadcast_search_inprogress": False,
    "broadcast_search_deadline": 0,
    "broadcast_search_term": "",
    "last_broadcast_search_at": 0,
    "fetch_feature_disabled": False,
}


# Where a write from a thread that outlived its test goes. One path for the
# whole run, inside the system temp directory, and never created: the point is
# that it is not data/, not that anything reads it.
_ORPHANED_WRITE_DIR = os.path.join(
    tempfile.gettempdir(), "dccore-orphaned-test-write")
_ORPHANED_WRITE_SINK = os.path.join(_ORPHANED_WRITE_DIR, "fetch_history.json")

# The queue file needs one of its own, and for a worse reason than the fetch
# history did.
#
# dcc.py's dispatch runs on threads that outlive the test which started them -
# tests/test_queue_progress_is_recorded.py and the #430 tests both start a real
# start_dcc_send() - and every path out of it settles the queue row through
# db.save_dcc_queue(). A thread still finishing after teardown restored the
# REAL db.DCC_QUEUE_FILE therefore writes the operator's own
# data/dcc_queue.txt. It was observed writing a two-byte file: an EMPTY queue,
# i.e. every queued transfer on that install silently dropped by running the
# test suite.
#
# Same treatment #415 gave the fetch history: a late write lands on a dead
# path nobody reads, and the real one is never a target at any point.
_ORPHANED_QUEUE_SINK = os.path.join(_ORPHANED_WRITE_DIR, "dcc_queue.txt")

# The same outliving start_dcc_send() thread that settles the queue row
# through db.save_dcc_queue() also settles the TRANSFER's own outcome, on
# success, through two more writers on the identical path:
# db.update_stats_on_complete() (dcc.py:2538) writes SPEED_RECORD_FILE, and
# db.record_download() (dcc.py:2554) writes DOWNLOAD_COUNTS_FILE. Both were
# still being restored to the real path in tearDown() below when the queue
# file's hole was found and closed - which means a late tick from the exact
# same thread could silently overwrite the operator's own speed record or
# download-count history the same way it emptied the queue.
_ORPHANED_SPEED_RECORD_SINK = os.path.join(_ORPHANED_WRITE_DIR, "speed_record.txt")
_ORPHANED_DOWNLOAD_COUNTS_SINK = os.path.join(_ORPHANED_WRITE_DIR, "download_counts.json")


def reset_config(**overrides):
    """Return config to a known-clean state, then apply any overrides.

    Locks are allocated here because oserve.py normally does it at startup and the
    tests do not run oserve.
    """
    for name, factory in RUNTIME_CONTAINERS.items():
        canonical = getattr(runtime, name, None)
        if canonical is None:
            # Not one of runtime.py's containers - send_queue and
            # user_processing_lock still live elsewhere - so a fresh object
            # is the right reset for them.
            setattr(config, name, factory())
            continue
        # For runtime.py's containers, empty the canonical object and point
        # config's name back at it. Emptying alone is not enough: a test may
        # have rebound config.<name> to a fixture of its own, and unless that
        # name is brought back to the shared object the next test starts
        # detached from runtime.py and resets would stop reaching it.
        if isinstance(canonical, dict):
            canonical.clear()
        else:
            del canonical[:]
        setattr(config, name, canonical)
    for name, value in SETTINGS_DEFAULTS.items():
        setattr(config, name, value)
    for name, value in RUNTIME_FLAGS.items():
        setattr(config, name, value)

    # A FRESH OUTBOUND CLOCK PER TEST. runtime.outbound_pacer is a
    # process-wide singleton holding "the earliest moment the next line may
    # leave", so a test that sends anything leaves the next test's first send
    # blocked for up to MSG_DELAY - five seconds by default. That is leaked
    # state like any other, and it costs real wall-clock on every suite run.
    # test_a_shared_outbound_pace.py already did this by hand for itself.
    runtime.outbound_pacer = runtime.OutboundPacer()

    config.debug_flood_lock = threading.Lock()
    config.fetch_queue_lock = threading.Lock()
    config.fetched_bot_lists_lock = threading.Lock()

    config.NICKNAME = "DCCore"
    config.ORIGINAL_NICK = "DCCore"
    config.LIST_BASE_NAME = "DCCore"
    config.ALT_NICKNAME = "DCCore_"
    config.ADMIN_NICK = "SysOp"
    config.MY_IP_OR_DOCK = "203.0.113.7"
    # #170's RFC: CHANNEL and FILE_DIRECTORY are in settings_file.REQUIRED and
    # ship blank (None) in config.py, same reason NICKNAME/ADMIN_NICK above
    # are reset explicitly here rather than left at their (now blank) shipped
    # default - a test that never sets these itself must still see a real,
    # non-blank value, matching how the whole suite already behaved before
    # REQUIRED existed. A path that does not exist is the right baseline for
    # FILE_DIRECTORY: that already matched the shipped default's own real-
    # world behaviour on any machine that is not the operator's own NAS, and
    # tests/test_startup.py's BootCase (and anything else that needs a real,
    # existing directory) already overrides this via make_tree().
    config.CHANNEL = "#dccore-test"
    config.FILE_DIRECTORY = "/nonexistent-dccore-test-directory"
    for stale in ("PREVIOUS_NICK",):
        if hasattr(config, stale):
            delattr(config, stale)

    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class RecordingSocket:
    """Stands in for the live IRC socket and remembers what was written to it."""

    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)
        return len(payload)

    def sendall(self, payload):
        self.sent.append(payload)

    def close(self):
        pass

    def text(self):
        return b"".join(
            p if isinstance(p, bytes) else str(p).encode("utf-8", "ignore")
            for p in self.sent
        ).decode("utf-8", "ignore")


class DeadSocket:
    """A socket whose peer has gone away, as after a netsplit."""

    def send(self, payload):
        raise OSError("Broken pipe")

    def sendall(self, payload):
        raise OSError("Broken pipe")

    def close(self):
        pass


def install_fake_oserve(irc_connection=None):
    """Install a stub ``oserve`` module and return it.

    The real oserve is the process entry point, and it IS imported here -
    list.py imports it, and announce imports list - but importing it runs
    nothing (startup() runs only under __main__, and since #707 so do the
    console installs). Everything else reaches oserve through
    sys.modules.get('oserve'), so a stub in its place is what the tests
    talk to, and what they observe.
    """
    stub = types.ModuleType("oserve")
    stub.irc_connection = irc_connection
    stub.active_downloads = 0
    stub.send_fails_count = 0
    stub.total_sent_bytes = 0
    stub.bot_joined_channel = True
    stub.queued = []

    def queue_message(user, message, is_vip=False):
        stub.queued.append((user, message, is_vip))

    stub.queue_message = queue_message
    sys.modules["oserve"] = stub
    return stub


def silence_debug(announce_module):
    """Capture announce.send_debug instead of writing to a socket.

    Returns the list the calls land in. send_debug is called from the IRC read
    thread and paces itself, so leaving it live would make tests slow and flaky.
    """
    captured = []

    # Signature kept in step with announce.send_debug() itself. A double
    # that is narrower than the real thing turns "a caller started
    # passing a new argument" into a TypeError inside unrelated tests,
    # reported as whatever those tests were actually about.
    def fake_send_debug(msg_text, category="INFO", notice=None):
        captured.append((category, msg_text))

    announce_module.send_debug = fake_send_debug
    return captured


def no_disk_writes(db_module):
    """Stop a test touching the real data/ directory."""
    db_module.save_dcc_queue = lambda: None
    db_module.save_bans_to_file = lambda: None
    db_module.save_advanced_stats = lambda stats: None


def queue_row(user="dave", filename="Song.flac", **extra):
    """Build a dcc_queue entry in the shape dcc.py actually creates."""
    row = {
        "file": filename,
        "path": "/srv/library/Artist/Album/" + filename,
        "channel": "#dccore-test",
        "user_raw": user,
        "is_temporary_zip": False,
    }
    row.update(extra)
    return row


class CapturedDispatch:
    """Intercept start_dcc_send so a test can assert what WOULD have been sent.

    Patches Thread on the real ``threading`` module rather than ``dcc.threading``.
    check_queue_and_send, start_dcc_send and handle_download_request each do a
    function-local ``import threading``, which resolves through sys.modules - so a
    patch on the dcc module attribute is never consulted and the real transfer runs
    against a real socket.

    Only threads whose target is start_dcc_send are intercepted; everything else is
    constructed normally, so the daemon's own worker threads still behave.
    """

    def __init__(self, dcc_module):
        self.dcc = dcc_module
        self.calls = []
        self._real_thread = threading.Thread

    def __enter__(self):
        real_thread = self._real_thread
        calls = self.calls

        class Intercept(real_thread):
            def __init__(self, target=None, args=(), **kwargs):
                if getattr(target, "__name__", "") == "start_dcc_send":
                    # args: (irc_sock, user, file_path, file_name, channel, next_file)
                    calls.append({"user": args[1], "path": args[2], "file": args[3],
                                  "entry": args[5] if len(args) > 5 else None})
                    target, args = (lambda: None), ()
                real_thread.__init__(self, target=target, args=args, **kwargs)

        threading.Thread = Intercept
        self.dcc.threading.Thread = Intercept
        return self

    def __exit__(self, *exc):
        threading.Thread = self._real_thread
        self.dcc.threading.Thread = self._real_thread
        return False

    @property
    def users(self):
        return [c["user"] for c in self.calls]

    @property
    def files(self):
        return [c["file"] for c in self.calls]


class TempTree:
    """A throwaway music library and lists directory.

    NOTE THE CASE: `music` and `lists` are lowercase, and a test that wants a
    directory of its own must CREATE it rather than assume a differently-cased
    spelling resolves to one of these. On NTFS it does; on the ubuntu runner
    it does not, so such a test passes on the developer's machine and fails
    every CI run. That has happened once already - see
    tests/test_audit_multilist_and_thaw.py's TheDownloadCounterKeepsARelativeKey.


    Uses real files because several of the behaviours under test are about the
    filesystem itself - path containment, atomic replacement, long names.
    """

    def __init__(self, tracks=("01 - Enter Sandman.flac", "02 - Sad But True.flac")):
        self.root = tempfile.mkdtemp(prefix="dccore-test-")
        self.music = os.path.join(self.root, "music")
        self.lists = os.path.join(self.root, "lists")
        self.album = os.path.join(self.music, "Metallica", "Black Album (1991)")
        os.makedirs(self.album)
        os.makedirs(self.lists)
        self.tracks = []
        for name in tracks:
            path = os.path.join(self.album, name)
            with open(path, "wb") as handle:
                handle.write(b"\x00" * 4096)
            self.tracks.append(path)
        # A sibling sharing the music root's prefix, for containment checks.
        self.sibling = self.music + "-backup"
        os.makedirs(self.sibling)
        # Something worth stealing, outside the jail.
        self.secret = os.path.join(self.root, "secret")
        os.makedirs(self.secret)
        with open(os.path.join(self.secret, "id_rsa"), "w") as handle:
            handle.write("PRIVATE KEY")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class DCCoreTestCase(unittest.TestCase):
    """Base case: clean config and a stub oserve for every test."""

    def setUp(self):
        restore_daemon_functions()
        self.config = reset_config()

        # THE LAST CLEANUP TO RUN, because it is registered first and unittest
        # runs them LIFO. Every set_config() restore below is registered later
        # and therefore runs earlier, putting the real paths back; this then
        # takes them away again.
        #
        # Why it has to exist at all: db.py derives DCC_QUEUE_FILE,
        # SPEED_RECORD_FILE and DOWNLOAD_COUNTS_FILE from config at IMPORT,
        # and a !rehash reloads db - so a reload re-derives all three from
        # whatever config says at that moment. dcc.py's start_dcc_send() also
        # dispatches on threads that outlive the test that started them, and
        # every exit path settles the queue row through db.save_dcc_queue() -
        # while a SUCCESSFUL one additionally settles the transfer itself
        # through db.update_stats_on_complete() and db.record_download().
        # Put those together and a late thread, after a reload, writes the
        # operator's own data/dcc_queue.txt, data/speed_record.txt or
        # data/download_counts.json. The queue file was caught writing two
        # bytes: an EMPTY queue, i.e. running the suite on a live install
        # silently drops every transfer anybody had queued. The other two
        # write from the identical thread and were found by inspection
        # rather than by being caught outright - not yet observed corrupting
        # anything is not the same claim as safe.
        #
        # #415 gave the fetch history the same treatment for the same reason.
        self.addCleanup(self._park_thread_written_files_on_dead_paths)
        # NO TEST MAY WRITE THE OPERATOR'S OWN settings.conf. Anything that
        # reaches settings_file.save() - the /api/settings route most
        # obviously - writes DEFAULT_PATH unless this variable says otherwise,
        # and that file is gitignored, so the damage does not show up in
        # `git status`: it shows up as unrelated tests failing later, on a
        # different branch, for reasons that have nothing to do with them.
        #
        # That is not hypothetical. Two route tests posting to /api/settings
        # left MAX_DCC_SLOTS and SERVER in the real file, and the next
        # preflight failed on main with a JSON decode error in the import
        # graph - because the subprocess it reads stdout from had started
        # printing "[CONFIG] Wrote 1 setting(s)".
        settings_home = tempfile.mkdtemp(prefix="dccore-settings-")
        self.addCleanup(shutil.rmtree, settings_home, ignore_errors=True)
        previous_settings_file = os.environ.get("DCCORE_SETTINGS_FILE")
        os.environ["DCCORE_SETTINGS_FILE"] = os.path.join(
            settings_home, "settings.conf")
        self.addCleanup(
            lambda: os.environ.__setitem__("DCCORE_SETTINGS_FILE",
                                           previous_settings_file)
            if previous_settings_file is not None
            else os.environ.pop("DCCORE_SETTINGS_FILE", None))

        self.oserve = install_fake_oserve()
        self._trees = []
        # dcc_fetch.check_fetch_queue() persists finished fetches to disk on
        # every tick it runs (dcc_fetch._persist_fetch_history_locked()), and
        # reset_config() leaves fetch_feature_disabled False - so the many
        # tests across this suite that call check_fetch_queue() would
        # otherwise write straight into the real repository's
        # data/fetch_history.json. Redirect it to a throwaway path, and
        # reset the "what did we last write" cache so one test's leftover
        # state can never mask another's.
        import db
        import dcc_fetch
        self._fetch_history_dir = tempfile.mkdtemp(prefix="dccore-fetch-history-")
        self._real_fetch_history_file = db.FETCH_HISTORY_FILE
        db.FETCH_HISTORY_FILE = os.path.join(self._fetch_history_dir, "fetch_history.json")
        # Fourth file, same rule. This one is easy to write by accident:
        # announce.record_notice() persists on every call, and it is reached
        # from send_debug(notice=...) - so any test exercising a kick, a
        # rejoin or a failed rebuild writes it without mentioning notices at
        # all, and the next run of the suite would start with the previous
        # run's badge already showing.
        self._real_notices_file = db.NOTICES_FILE
        db.NOTICES_FILE = os.path.join(self._fetch_history_dir, "notices.json")
        # Fifth file, same rule. Reached from the IRC read loop, so any test
        # that feeds it a private message writes this without mentioning it.
        self._real_pm_file = db.PRIVATE_MESSAGES_FILE
        db.PRIVATE_MESSAGES_FILE = os.path.join(self._fetch_history_dir,
                                                "private_messages.json")
        dcc_fetch._last_persisted_terminal_snapshot = {}
        # Same reason, for the bot registry. oserve.start() loads it at boot,
        # so every test that boots the daemon was reading whatever bots this
        # machine's own bot has met - which made the suite's behaviour depend
        # on live local data, passing on CI where the file does not exist and
        # failing here. The registry is gitignored, so nobody saw it until a
        # test asserted on the contents of that dict.
        # Same rule as DCCORE_SETTINGS_FILE above: no test may write a real
        # file under data/. This one holds an X login in plain text, so a test
        # that wrote it would put a password in the developer's working
        # directory - and data/ is gitignored, so it would not show up in
        # `git status` any more than settings.conf did.
        self.set_config(ON_CONNECT_FILE=os.path.join(self._fetch_history_dir,
                                                     "on_connect.json"))

        # Third file, same rule, and it got in the same way: a test called
        # library.save_lists() and wrote data/lists.json for real. The paths
        # in it were that test's temp directory, deleted the moment it ended -
        # so every LATER test read a lists.json defining folders that no
        # longer exist. It cost 147 failures and 27 errors in one preflight
        # run, all of them in tests that never mentioned lists.
        #
        # data/ is gitignored, so `git status` was clean throughout. That is
        # the third time this exact shape has bitten: settings.conf, then
        # on_connect.json, now lists.json. The rule is the file, not the
        # feature - anything a test can persist has to be redirected here.
        self.set_config(LISTS_FILE=os.path.join(self._fetch_history_dir,
                                                "lists.json"))
        self.set_config(LIBRARY_FOLDERS_FILE=os.path.join(
            self._fetch_history_dir, "library_folders.json"))
        # A DIRECTORY this time (#643): oserve.startup() makedirs
        # FETCHED_FILES_DIR, so every test that boots the daemon for real
        # created data/fetched in the operator's tree - and the preflight
        # guard, which walked files only, could not see an empty directory.
        # Not created here: the code under test is what creates it, and a
        # test that wants to see that happen can.
        self.set_config(FETCHED_FILES_DIR=os.path.join(
            self._fetch_history_dir, "fetched"))

        self._real_known_bots_file = db.KNOWN_BOTS_FILE
        db.KNOWN_BOTS_FILE = os.path.join(self._fetch_history_dir,
                                          "known_bots.json")

        # The console's token store (#704, audit L40). Every password check
        # goes through db.load_admin_tokens() on this path, so every login
        # test read the OPERATOR'S data/adminchat_tokens.json from the cwd
        # - each wrong-password test verified PBKDF2 against every real
        # token - and a `pair` reached from any test but the two that
        # redirected it themselves would have written the live store.
        self._real_admin_tokens_file = db.ADMIN_TOKENS_FILE
        db.ADMIN_TOKENS_FILE = os.path.join(self._fetch_history_dir,
                                            "adminchat_tokens.json")
        self.set_config(ADMIN_TOKENS_FILE=db.ADMIN_TOKENS_FILE)

        # The single-instance lock (#710): oserve.startup() takes it beside
        # the queue file - in this test's temp tree, since DCC_QUEUE_FILE is
        # redirected above - and holds it for the process. Released here so
        # the next test that boots is not refused as a second instance.
        import platform_compat as _platform_compat
        self.addCleanup(_platform_compat.release_instance_lock)

        # Same shape, found the same way: the state guard caught it the first
        # time a test drove a transfer all the way to completion, because
        # db.record_download() is only reached on the success path and
        # nothing had ever taken one. A module-level constant like the two
        # above, so it is rebound here and restored in tearDown.
        self._real_download_counts_file = db.DOWNLOAD_COUNTS_FILE
        db.DOWNLOAD_COUNTS_FILE = os.path.join(self._fetch_history_dir,
                                               "download_counts.json")

        # And five more the new preflight state guard found the moment it
        # existed: bans.txt, dcc_queue.txt, fetched_bot_lists.json,
        # list_index.db and stats.txt were all being written for real by the
        # suite. Nobody had noticed, because data/ is gitignored.
        #
        # It is worse than untidy. The daemon runs from its own directory on
        # the production LXC, so running the suite there overwrote the live
        # bot's queue, its ban list and its accumulated stats with test
        # fixtures - the exact totals the OmenServe import exists to preserve.
        #
        # db.DCC_QUEUE_FILE and db.FETCHED_BOT_LISTS_FILE are module-level
        # constants read at import, so they are rebound directly and restored
        # in tearDown; the rest are config values and go through set_config().
        # AND the config values behind them, not only the module constants.
        # db.py derives each of these once at import - FETCH_HISTORY_FILE =
        # getattr(config, "FETCH_HISTORY_FILE", "data/fetch_history.json") -
        # and a !rehash reloads db, which re-runs that line. Any test that
        # exercises a reload therefore threw the redirect away mid-run and
        # every later write in that process went to the developer's real
        # data/ directory. defaults.py does not define these names, so the
        # fallback is the real path; setting them on config means the reload
        # re-derives the temp one instead.
        #
        # Found by the preflight state guard, on a run where nothing else had
        # changed - which is exactly the kind of intermittent leak it exists
        # to make loud.
        self.set_config(
            FETCH_HISTORY_FILE=os.path.join(self._fetch_history_dir,
                                            "fetch_history.json"),
            SPEED_RECORD_FILE=os.path.join(self._fetch_history_dir,
                                           "speed_record.txt"),
            LIST_PROGRESS_FILE=os.path.join(self._fetch_history_dir,
                                            "list_progress.json"),
            KNOWN_BOTS_FILE=os.path.join(self._fetch_history_dir,
                                         "known_bots.json"),
            DCC_QUEUE_FILE=os.path.join(self._fetch_history_dir,
                                        "dcc_queue.txt"),
            FETCHED_BOT_LISTS_FILE=os.path.join(self._fetch_history_dir,
                                                "fetched_bot_lists.json"))

        # Ninth file, found the same way as the previous five: a new test
        # wrote the real one and the leak showed up as a value bleeding
        # between tests. db.SPEED_RECORD_FILE is the bot's all-time record -
        # exactly the kind of accumulated number an operator cannot get back,
        # and the sort the OmenServe import exists to carry across.
        self._real_speed_record_file = db.SPEED_RECORD_FILE
        db.SPEED_RECORD_FILE = os.path.join(self._fetch_history_dir,
                                            "speed_record.txt")

        self._real_dcc_queue_file = db.DCC_QUEUE_FILE
        db.DCC_QUEUE_FILE = os.path.join(self._fetch_history_dir, "dcc_queue.txt")
        self._real_fetched_bot_lists_file = db.FETCHED_BOT_LISTS_FILE
        db.FETCHED_BOT_LISTS_FILE = os.path.join(self._fetch_history_dir,
                                                 "fetched_bot_lists.json")
        self.set_config(
            BANS_FILE=os.path.join(self._fetch_history_dir, "bans.txt"),
            STATS_FILE=os.path.join(self._fetch_history_dir, "stats.txt"),
            LIST_INDEX_FILE=os.path.join(self._fetch_history_dir, "list_index.db"))

    def tearDown(self):
        restore_daemon_functions()
        # The cross-list index caches ONE sqlite connection at module level and
        # keeps it for the life of the process, which is right for the daemon
        # and wrong for a test run: a test that opens one indirectly - through
        # a fetch completing, or a dashboard route - leaves it open, pointing
        # at a temp directory this teardown is about to delete. On Windows that
        # is a locked file in a directory being removed, and the connection
        # survives to interpreter shutdown, where it surfaces as a
        # ResourceWarning with no test name attached to it.
        import list_index
        list_index.close()

        for tree in self._trees:
            tree.cleanup()
        import db
        # NOT RESTORED to the real path, deliberately. dcc_fetch's dispatcher
        # persists the fetch history every 2s on a daemon thread that outlives
        # the test that started it, so restoring the real path here opens a
        # window: a tick landing between this line and the end of the run
        # writes the operator's own data/fetch_history.json. That is what
        # preflight's state-write guard kept catching - intermittently, because
        # it needs a 2s tick to land inside a teardown, which is exactly the
        # kind of failure that reads as a flake and is not one.
        #
        # Pointed at a dead temp path instead: a late tick then fails to write
        # a file nobody reads, which costs nothing, while the real one is never
        # a target at any point in the run.
        db.FETCH_HISTORY_FILE = _ORPHANED_WRITE_SINK
        db.NOTICES_FILE = self._real_notices_file
        db.PRIVATE_MESSAGES_FILE = self._real_pm_file
        db.KNOWN_BOTS_FILE = self._real_known_bots_file
        db.ADMIN_TOKENS_FILE = self._real_admin_tokens_file
        # NOT self._real_download_counts_file / self._real_speed_record_file
        # / self._real_dcc_queue_file - see the three _ORPHANED_*_SINK
        # constants above. A start_dcc_send() thread still settling its
        # queue row, its stats or its speed record after this line would
        # otherwise write into the operator's own data/ files.
        db.DOWNLOAD_COUNTS_FILE = _ORPHANED_DOWNLOAD_COUNTS_SINK
        db.DCC_QUEUE_FILE = _ORPHANED_QUEUE_SINK
        db.SPEED_RECORD_FILE = _ORPHANED_SPEED_RECORD_SINK
        db.FETCHED_BOT_LISTS_FILE = self._real_fetched_bot_lists_file
        shutil.rmtree(self._fetch_history_dir, ignore_errors=True)

    def _park_thread_written_files_on_dead_paths(self):
        """Point every name a start_dcc_send() thread can still reach, after
        this test has already finished, at a sink instead of the real file.

        BOTH names per file: db.X is what the writer reads, and config.X is
        what a db reload (a !rehash test causes one) re-derives it from.
        Leaving either one on the real path leaves the hole open. All three
        files are settled from the same outliving thread - the queue row
        unconditionally, the other two only when the transfer succeeded -
        so all three get the identical treatment.
        """
        import db as _db

        for name, sink in (
            ("DCC_QUEUE_FILE", _ORPHANED_QUEUE_SINK),
            ("SPEED_RECORD_FILE", _ORPHANED_SPEED_RECORD_SINK),
            ("DOWNLOAD_COUNTS_FILE", _ORPHANED_DOWNLOAD_COUNTS_SINK),
        ):
            setattr(_db, name, sink)
            setattr(self.config, name, sink)

    def set_config(self, **overrides):
        """Set config attributes for the duration of one test, restoring
        whatever was there before (or removing the attribute if it did not
        exist) on teardown.

        For tunables reset_config() does not already reset -
        MAX_FETCH_SLOTS, MAX_FETCH_FILE_SIZE and similar plain config.py
        literals are module-level state shared across the whole test run,
        not part of RUNTIME_CONTAINERS/RUNTIME_FLAGS - so a test that sets
        one directly and never restores it silently changes every test that
        runs afterwards in the same process.
        """
        for name, value in overrides.items():
            had_value = hasattr(config, name)
            old_value = getattr(config, name, None)
            setattr(config, name, value)
            if had_value:
                self.addCleanup(setattr, config, name, old_value)
            else:
                self.addCleanup(delattr, config, name)

    def make_tree(self, **kwargs):
        tree = TempTree(**kwargs)
        self._trees.append(tree)
        self.config.FILE_DIRECTORY = tree.music
        self.config.LOCAL_LIST_DIR = tree.lists
        self.config.TMP_ZIP_DIR = os.path.join(tree.root, "tmp_zips")
        return tree
