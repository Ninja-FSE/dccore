"""Shared scaffolding for the DCCore test suite.

The daemon keeps all of its live state in ``config`` as module globals, and its
modules reach for each other through ``sys.modules`` at call time rather than
through imports. That is fine for a long-running process and awkward for tests,
so everything needed to put the modules into a known state lives here.

Deliberately stdlib-only. The daemon itself has no dependencies and runs in a
minimal LXC, so the tests must run there too - and on Windows, where the port is
headed - with nothing more than a Python install.
"""

import ast
import gc
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


# Parsed source, shared by every source-reading test in the process (#1147).
# Those tests re-parsed the same big modules thousands of times a run - irc.py
# was opened 210 times, and one parse of it costs 50 to 150 ms - when each
# distinct text needs parsing once.
#
# KEYED ON THE TEXT ITSELF, never on a path and its mtime: several tests
# write a file and scan it again, and a rewrite inside one timestamp tick
# would hand back the old tree - the same trap as a stale __pycache__.
# Reading the text again is cheap; parsing it is what costs.
#
# The trees are SHARED: a test must read them and never change them.
# tests/test_source_reading_tests_parse_each_text_once.py holds the suite to
# that. Texts under _PARSE_CACHE_MIN_CHARS parse in well under a
# millisecond and are not kept.
_PARSED_SOURCES = {}
_PARSE_CACHE_MIN_CHARS = 10000


def parse_source(text, filename="<unknown>"):
    """ast.parse(text, filename), parsed once per process for the same text.

    The tree returned may be the one another test was given: read it, walk
    it, never change it.
    """
    if len(text) < _PARSE_CACHE_MIN_CHARS:
        return ast.parse(text, filename=filename)
    key = (filename, text)
    tree = _PARSED_SOURCES.get(key)
    if tree is None:
        tree = _PARSED_SOURCES[key] = ast.parse(text, filename=filename)
    return tree


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
    "queue_waiting_since": dict,
    "channel_users": dict,
    "user_requests": dict,
    "muted_until": dict,
    "whois_status": dict,
    "failed_transfers": dict,
    "vip_queue": list,
    "fetch_request_queue": list,
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
    # #926: who else asked which bot for its list. A leftover is a bot the
    # next test's automatic grab leaves alone for no reason it can see.
    "list_grab_others_asked": dict,
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
    "recent_departure_bases": dict,
    "nick_aliases": dict,
    # #376 option B: a leftover ident or departure is a merge the next test
    # never set up.
    "bot_idents": dict,
    "bot_departures": dict,
    "recent_joins": dict,
    # #371: one test's chat lines or limits are not the next one's.
    "chat_recent": list,
    "chat_rate": dict,
    "chat_outbound": dict,
    "chat_muted": dict,
    "chat_peers": dict,
    "chat_peers_meta": dict,
    "chat_who_round": dict,
    # #1066: one test's modes or resend count are not the next one's.
    "on_connect_state": dict,
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
    # The daily version check (#572) ships ON, and a test that boots the
    # daemon would start its worker - which one day asks GitHub. Off here,
    # so no test ever holds that thread; the ones that exercise the check
    # turn it on themselves and give it a fake GitHub.
    "CHECK_FOR_UPDATES": False,
    # Shipped blank. Eleven tests set a dashboard and console password, and
    # every later test in the process ran with one configured: webserver.start()
    # and the console's "no password configured" refusal answered differently
    # depending on which module happened to run before.
    "ADMIN_PASSWORD_HASH": "",
    # Shipped blank, which drops channel debug lines (#424). A test that named
    # a debug channel left every later send_debug() queueing channel lines.
    "DEBUG_CHANNEL": "",
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
_ORPHANED_TRANSFER_LOG_SINK = os.path.join(_ORPHANED_WRITE_DIR, "transfers.db")


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
    runtime.known_bots_pruned_at = 0.0
    for name, value in SETTINGS_DEFAULTS.items():
        setattr(config, name, value)
    for name, value in RUNTIME_FLAGS.items():
        setattr(config, name, value)

    # Which bots the fetch dispatcher saw leave, and when each came back
    # (#926). Process-long in dcc_fetch; one test's absent bot would give the
    # next test's a "just back" delay, depending only on test order.
    fetch_module = sys.modules.get("dcc_fetch")
    if fetch_module is not None:
        fetch_module._seen_absent.clear()
        fetch_module._back_since.clear()
        fetch_module._paused.clear()
        fetch_module._connect_failures.clear()
        fetch_module._disk_was_low[0] = False

    # The dashboard's failed-login counts. A test that posts a wrong password
    # left 127.0.0.1 one attempt closer to its block (MAX_PASSWORD_ATTEMPTS
    # is 3, for 900 s), and the next tests that log in without clearing the
    # pool themselves would then get 401 for a password they typed right.
    webserver_module = sys.modules.get("webserver")
    if webserver_module is not None:
        webserver_module._web_bad_ips.clear()

    # What the version check (#572) last found. Read from runtime.py itself,
    # not through config, so reset there: a release "found" by one test would
    # otherwise be the next test's `status` line.
    for name, value in (("update_check_started", False), ("update_check_last_attempt", None),
                        ("update_check_last_manual", None), ("update_check_at", None),
                        ("update_check_error", None), ("update_check_error_at", None),
                        ("update_check_latest", None), ("update_check_url", None),
                        ("update_check_newer", False), ("update_check_announced", None),
                        # #926: automatic list grabbing's wait, last grab and
                        # per-bot record - reloaded from the test's own file.
                        ("list_grab_started", False), ("list_grab_plan", None),
                        ("list_grab_last", None), ("list_grab_state", None),
                        ("chat_last_id", 0),
                        # Stamped by dcc.pause_freeze_clock() when a test drives
                        # irc_loop() to its disconnect epilogue. Left set, the
                        # next test whose run reaches activation calls
                        # resume_freeze_clock(), which pushes every frozen
                        # queue's timestamp forward by the whole time since the
                        # earlier test - minutes - so a queue that test expects
                        # to expire is kept.
                        ("freeze_clock_paused_at", None),
                        # #1182: one test's background audio reading, or
                        # its result, is not the next one's.
                        ("audio_reading", None), ("audio_reading_last", None),
                        ("audio_retry_waiting", False)):
        setattr(runtime, name, value)

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


# Every setting reset_config() above puts back on every call, and the value
# it puts back. For the leak guard in tests/__init__.py:
#   - a DCCoreTestCase test that changes one of these and leaves it is not
#     held to account: the next DCCoreTestCase starts from the reset value
#     whatever it was left at;
#   - no test is held to account for leaving one AT its reset value. A plain
#     TestCase that calls reset_config() itself changes them from whatever
#     the test before left, which is the reset doing its job.
# tests/test_a_test_leaves_the_process_as_it_found_it.py checks that
# reset_config() really sets each of these to the value given here.
SETTINGS_RESET_VALUES = dict(SETTINGS_DEFAULTS, NICKNAME="DCCore", ORIGINAL_NICK="DCCore",
                             LIST_BASE_NAME="DCCore", ALT_NICKNAME="DCCore_", ADMIN_NICK="SysOp",
                             MY_IP_OR_DOCK="203.0.113.7", CHANNEL="#dccore-test",
                             FILE_DIRECTORY="/nonexistent-dccore-test-directory")
SETTINGS_RESET_FOR_EVERY_TEST = frozenset(SETTINGS_RESET_VALUES)


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


def bots_in_the_channel(*nicks, channel="#chan"):
    """We have joined, and `nicks` are in `channel`: what the fetch dispatcher
    waits for before it asks anybody (#965). Lower-cased, as irc.py keeps
    config.channel_users."""
    config.bot_joined_channel = True
    config.channel_users.setdefault(channel.lower(), set()).update(str(n).lower() for n in nicks)


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


# THE SWEEPS A REAL SEND SCHEDULES FOR LATER.
#
# Every path out of dcc.start_dcc_send() starts one more thread on its way
# out: delayed_queue_trigger_fallback sleeps 3 s (15 s per failure when the
# row is kept) and then calls check_queue_and_send(); the no-free-port branch
# starts delayed_port_retry, which does the same after 45 s. Nothing joined
# them. Each one woke inside whatever test was running by then and swept THAT
# test's queue: it thawed or dropped a frozen queue, dispatched a queued row,
# started a second freeze countdown, or landed in a recording stub another
# test had put on dcc.check_queue_and_send. That is the "a background thread
# re-froze a queue" flake; #642 and 9ee9c541 each hardened one victim.
SEND_FOLLOW_UPS = ("delayed_queue_trigger_fallback", "delayed_port_retry")

# How long a cleanup waits for a held thread that was already past its sleep
# when the test ended: a pass over the queue, not a transfer.
_HELD_THREAD_JOIN_SECONDS = 10.0


class _SleepThatEndsWithTheTest:
    """`time`, as one held thread sees it: its sleep is cut short the moment
    the test that started it ends. Everything else is the real module."""

    def __init__(self, ended):
        self._ended = ended

    def sleep(self, seconds):
        self._ended.wait(seconds)

    def __getattr__(self, name):
        import time
        return getattr(time, name)


def hold_threads_until_the_test_ends(test, targets, skipped_once_it_has=()):
    """Keep the threads `test` starts on a function named in `targets` inside
    the test.

    While the test runs they behave as in production: the same sleeps, the
    same calls. When it ends, a sleep still running returns at once, every
    function named in `skipped_once_it_has` does nothing from then on, and
    each thread is joined before the next test starts.

    How: when such a thread starts, its target is rebuilt so that `time` and
    each skipped name resolve to stand-ins - a module global or a variable of
    the enclosing function (the daemon often imports time inside a function),
    whichever the code uses. The stand-in for a skipped function looks the
    real one up when it is called, as the original did. The daemon's code is
    not touched, and the process-wide time.sleep, which every other thread
    uses, is not patched. A target that stops sleeping through `time.sleep`
    sleeps for real, and the leak guard in tests/__init__.py names it.

    Register it BEFORE the cleanup that joins whatever thread starts these:
    cleanups run in reverse order.
    """
    ended = threading.Event()
    held = []
    real_start = threading.Thread.start

    def rebuilt(target):
        source_globals = target.__globals__

        def skipped(name):
            def call(*args, **kwargs):
                if ended.is_set():
                    return None
                return source_globals[name](*args, **kwargs)
            return call

        replace = {"time": _SleepThatEndsWithTheTest(ended)}
        replace.update((name, skipped(name)) for name in skipped_once_it_has)
        scoped = dict(source_globals)
        scoped.update(replace)
        cells = tuple(types.CellType(replace[name]) if name in replace else cell
                      for name, cell in zip(target.__code__.co_freevars,
                                            target.__closure__ or ()))
        function = types.FunctionType(target.__code__, scoped, target.__name__,
                                      target.__defaults__, cells or None)
        function.held_by_a_test = True
        return function

    def start(thread):
        target = getattr(thread, "_target", None)
        # Held once, by the innermost test: a test that runs another test
        # inside itself has two of these patches stacked, and the inner one
        # is the one whose end the thread must not outlive.
        if (getattr(target, "__name__", None) in targets
                and not getattr(target, "held_by_a_test", False)):
            thread._target = rebuilt(target)
            held.append(thread)
        return real_start(thread)

    def release():
        ended.set()
        for thread in held:
            thread.join(_HELD_THREAD_JOIN_SECONDS)
        threading.Thread.start = real_start

    threading.Thread.start = start
    test.addCleanup(release)
    return held


def hold_send_follow_ups(test):
    """Keep the sweeps a real start_dcc_send() schedules inside `test`.

    While the test runs they behave as they do in production: the same
    delay, then the real dcc.check_queue_and_send() (so a test can still wait
    for one; test_the_queue_save_never_touches_the_live_dict does). When the
    test ends, a sweep still waiting stops waiting and sweeps nothing, and
    every one of them is joined, so none can reach a later test.

    DCCoreTestCase calls this in setUp. A plain unittest.TestCase that drives
    a real send calls it itself, before it starts the send: the sender's own
    finally is what starts the follow-up.
    """
    return hold_threads_until_the_test_ends(test, SEND_FOLLOW_UPS,
                                            skipped_once_it_has=("check_queue_and_send",))


def retire_the_debug_drain():
    """Stop announce's debug drain, if a test started one, and clear its line.

    send_debug() starts _debug_drain_worker once per process and latches
    _debug_drain_started; the worker loops for ever. Left running, it popped
    later tests' debug lines and wrote them to whichever socket a later test
    had published as oserve.irc_connection, and took that test's pacer slot.
    test_announce_output and test_theme both left one.

    A new _debug_drain_id is what the worker itself checks to retire
    (announce.py: "Superseded by a newer drain"), so it ends at its next
    pass, and the next real send_debug() starts a fresh one.
    """
    module = sys.modules.get("announce")
    if module is None:
        return
    with module._debug_drain_guard:
        if module._debug_drain_started:
            module._debug_drain_id = object()
            module._debug_drain_started = False


def remove_tree(path):
    """Remove a test's temp directory, and mean it (#1149).

    shutil.rmtree(path, ignore_errors=True) was the rule here, and on Windows
    it quietly left the whole directory behind whenever one file in it was
    still open - a sqlite connection nobody closed, its -wal beside it. A
    full run left about 236 directories in the temp folder, every run, and
    nothing said so: 232,000 had built up on one machine, and listing that
    folder had become slow enough to slow the tests that start a child
    process from it.

    So: try; if a file is still held, close the index connection the suite
    caches and try again; if that was not it, collect the connections
    nobody holds any more (a collected sqlite3.Connection closes its file)
    and try once more. Whatever still cannot go is left rather than raised:
    a cleanup must not fail a test that passed.
    """
    if not path or not os.path.lexists(path):
        return
    try:
        shutil.rmtree(path)
        return
    except OSError:
        pass
    try:
        import list_index
        list_index.close()
        shutil.rmtree(path)
        return
    except Exception:  # noqa: BLE001 - best effort; the last try below decides
        pass
    # A whole collection costs a moment on a heap this size, so only when
    # closing the index was not enough.
    gc.collect()
    shutil.rmtree(path, ignore_errors=True)


def temp_dir(test, prefix="dccore-test-"):
    """A fresh directory that is removed when `test` ends, however it ends.

    For any unittest.TestCase; DCCoreTestCase.make_temp_dir() is the same
    thing as a method. Registered with addCleanup, so a failing assertion
    or an error in setUp after this line still removes it (#1149).
    """
    path = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(remove_tree, path)
    return path


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
        remove_tree(self.root)


class DCCoreTestCase(unittest.TestCase):
    """Base case: clean config and a stub oserve for every test."""

    # Settings the harness itself sets, which the leak guard in
    # tests/__init__.py therefore does not count against the test: the ones
    # reset_config() resets for every test, and the four pointed at dead
    # sinks after every test, on purpose (_park_thread_written_files_on_dead_paths).
    SETTINGS_THE_HARNESS_SETS = SETTINGS_RESET_FOR_EVERY_TEST | frozenset((
        "DCC_QUEUE_FILE", "SPEED_RECORD_FILE", "DOWNLOAD_COUNTS_FILE", "TRANSFER_LOG_FILE"))

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
        # Registered before everything else below, so it runs after all of
        # it (#1149). Removing these in tearDown() ran BEFORE every
        # addCleanup() - before a test's own cleanup had closed the sqlite
        # file it opened in one of them - and on Windows that left the
        # directory behind, every time. Its directories are made further down.
        # The lists are handed to the cleanup, not read off self when it
        # runs: a test that calls setUp() a second time must not orphan the
        # first call's directories.
        self._made_dirs = []
        self._trees = []
        self.addCleanup(self._remove_made_dirs, self._made_dirs, self._trees)
        # Registered early, so both run after every cleanup the test adds -
        # in particular after the one that joins a sending thread, whose own
        # finally is what starts a follow-up sweep.
        self.addCleanup(retire_the_debug_drain)
        hold_send_follow_ups(self)
        # A line queued for the debug channel by an earlier test, and never
        # drained because no socket was published then, is not this test's.
        # A drain this test starts would deliver it as one of its own:
        # test_a_shared_outbound_pace counted 9 lines where it sent 8.
        announce._debug_queue.clear()
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
        self._made_dirs.append(settings_home)
        previous_settings_file = os.environ.get("DCCORE_SETTINGS_FILE")
        os.environ["DCCORE_SETTINGS_FILE"] = os.path.join(
            settings_home, "settings.conf")
        self.addCleanup(
            lambda: os.environ.__setitem__("DCCORE_SETTINGS_FILE",
                                           previous_settings_file)
            if previous_settings_file is not None
            else os.environ.pop("DCCORE_SETTINGS_FILE", None))

        self.oserve = install_fake_oserve()
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
        self._made_dirs.append(self._fetch_history_dir)
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
        # The list and archive folders, for a test that does not make a tree
        # of its own. make_tree() used to set these directly, so after any
        # test that called it both pointed into that test's deleted tree for
        # the rest of the process, a different dead path depending on which
        # test ran last. Putting the shipped ./lists and ./data/tmp_zips back
        # would be worse: they are the checkout's own folders. A path of this
        # test's own, not created, is neither.
        self.set_config(LOCAL_LIST_DIR=os.path.join(self._fetch_history_dir, "lists"),
                        TMP_ZIP_DIR=os.path.join(self._fetch_history_dir, "tmp_zips"))

        self._real_known_bots_file = db.KNOWN_BOTS_FILE
        db.KNOWN_BOTS_FILE = os.path.join(self._fetch_history_dir,
                                          "known_bots.json")
        self._real_list_grabs_file = db.LIST_GRABS_FILE
        db.LIST_GRABS_FILE = os.path.join(self._fetch_history_dir, "list_grabs.json")

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
        # above, so it is rebound here and restored in tearDown. Since #1133
        # the counts live in a database derived from this path (its extension
        # replaced by .db), so moving the path moves the database with it.
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
            # The operator's hand-kept ban list. Not written by the daemon,
            # but read on every request: a test that does not name one of its
            # own must not answer from this machine's. And eighty tests set
            # it directly to a file in their own temp tree; restored here,
            # they no longer leave the next test reading a deleted file.
            HARD_BANS_FILE=os.path.join(self._fetch_history_dir, "hard_bans.txt"),
            BANS_FILE=os.path.join(self._fetch_history_dir, "bans.txt"),
            STATS_FILE=os.path.join(self._fetch_history_dir, "stats.txt"),
            LIST_INDEX_FILE=os.path.join(self._fetch_history_dir, "list_index.db"),
            LIST_AUDIO_INFO_CACHE=os.path.join(self._fetch_history_dir, "audio_info.db"),
            TRANSFER_LOG_FILE=os.path.join(self._fetch_history_dir, "transfers.db"))

    def tearDown(self):
        restore_daemon_functions()
        # The cross-list index caches its sqlite connections at module level -
        # the writers' one, and the dashboard readers' own since #1129, which
        # close() closes with it - and keeps them for the life of the process,
        # which is right for the daemon and wrong for a test run: a test that
        # opens one indirectly - through a fetch completing, or a dashboard
        # route - leaves it open, pointing at a temp directory this teardown
        # is about to delete. On Windows that is a locked file in a directory
        # being removed, and the connection survives to interpreter shutdown,
        # where it surfaces as a ResourceWarning with no test name attached.
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
        db.LIST_GRABS_FILE = self._real_list_grabs_file
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
        # self._fetch_history_dir is removed by _remove_made_dirs(), the
        # last cleanup to run, not here (#1149).

    @staticmethod
    def _remove_made_dirs(made_dirs, trees):
        """Remove this test's redirect directories, the trees it made and
        the ones make_temp_dir() handed out (#1149). The last cleanup."""
        for tree in trees:
            tree.cleanup()
        for path in made_dirs:
            remove_tree(path)

    def make_temp_dir(self, prefix="dccore-test-"):
        """A fresh directory, removed after every other cleanup of this test (#1149)."""
        path = tempfile.mkdtemp(prefix=prefix)
        self._made_dirs.append(path)
        return path

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
        # transfer_log.py reads its path from config alone, at the moment of the write.
        self.config.TRANSFER_LOG_FILE = _ORPHANED_TRANSFER_LOG_SINK

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

    def keep_every_setting(self):
        """Put every setting back when this test ends, whichever of them it
        changed - for a test that applies a whole settings file to the live
        config, as the browser setup page does.

        Those tests put back the names they set themselves and nothing else,
        and apply_setup() also changes SERVER, CHANNEL and the rest: the next
        module to read SERVER's default got the form's example host. A plain
        run hid it, because the module that ran next in alphabetical order
        reloads defaults; a parallel run (#1146) put that module in another
        shard.
        """
        names = list(getattr(config, "SHIPPED_VALUES", {})) + ["ADMIN_PASSWORD_HASH"]
        self.set_config(**{name: getattr(config, name) for name in names if hasattr(config, name)})

    def make_tree(self, **kwargs):
        tree = TempTree(**kwargs)
        self._trees.append(tree)
        # Through set_config(), so the folders setUp gave this test come back
        # when it ends rather than this tree's, which is deleted by then.
        self.set_config(FILE_DIRECTORY=tree.music, LOCAL_LIST_DIR=tree.lists,
                        TMP_ZIP_DIR=os.path.join(tree.root, "tmp_zips"))
        return tree
