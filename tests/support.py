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
}

RUNTIME_FLAGS = {
    "search_inprogress": False,
    "rar_inprogress": False,
    "bot_joined_channel": True,
    "activation_triggered": False,
    "update_inprogress": False,
    "last_list_update_ok": None,
    "last_list_update_error": None,
    "connection_epoch": 1,
    "broadcast_search_inprogress": False,
    "broadcast_search_deadline": 0,
    "broadcast_search_term": "",
    "last_broadcast_search_at": 0,
    "fetch_feature_disabled": False,
}


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
    for name, value in RUNTIME_FLAGS.items():
        setattr(config, name, value)

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

    Real oserve is the process entry point; importing it would start worker
    threads. Everything else reaches it via sys.modules.get('oserve'), so a stub
    is enough and keeps the tests single-threaded unless a case asks otherwise.
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

    def fake_send_debug(msg_text, category="INFO"):
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
        dcc_fetch._last_persisted_terminal_snapshot = {}

    def tearDown(self):
        restore_daemon_functions()
        for tree in self._trees:
            tree.cleanup()
        import db
        db.FETCH_HISTORY_FILE = self._real_fetch_history_file
        shutil.rmtree(self._fetch_history_dir, ignore_errors=True)

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
