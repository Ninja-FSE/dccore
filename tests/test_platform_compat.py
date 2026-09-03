"""Tests for platform_compat - the few places Linux and Windows genuinely differ.

These run on both platforms in CI and assert the correct behaviour for whichever
one they are on, so a change that is right on Linux and wrong on Windows fails
before it merges rather than after somebody tries the port.
"""

import ast
import io
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import platform_compat  # noqa: E402


class RarCommandTests(unittest.TestCase):
    """dcc.py used the bare name "rar", which WinRAR does not put on PATH."""

    def test_configured_absolute_path_wins(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as handle:
            fake = handle.name
        try:
            self.assertEqual(platform_compat.rar_command(fake), fake)
        finally:
            os.unlink(fake)

    def test_configured_path_that_does_not_exist_falls_back_to_lookup(self):
        missing = os.path.join(tempfile.gettempdir(), "definitely-not-here-rar")
        # Falls through to the PATH search; on a machine without rar that is None,
        # and on one with it, whatever was found. Either way it must not return
        # the bogus configured value.
        self.assertNotEqual(platform_compat.rar_command(missing), missing)

    def test_returns_none_or_an_existing_file(self):
        """Never return a name that cannot actually be executed."""
        found = platform_compat.rar_command(None)
        if found is not None:
            self.assertTrue(os.path.isfile(found), f"returned {found!r} which is not a file")

    def test_no_argument_is_accepted(self):
        platform_compat.rar_command()


class ListenerOptionTests(unittest.TestCase):
    """SO_REUSEADDR has the OPPOSITE meaning on Windows - it permits hijacking."""

    def setUp(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(self.sock.close)

    def test_prepare_listener_returns_the_socket(self):
        self.assertIs(platform_compat.prepare_listener(self.sock), self.sock)

    def test_correct_option_for_this_platform(self):
        platform_compat.prepare_listener(self.sock)
        if platform_compat.IS_WINDOWS:
            # SO_EXCLUSIVEADDRUSE must be on, and SO_REUSEADDR must NOT be, or
            # another process could bind the same DCC port and take the transfer.
            self.assertTrue(
                self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE),
                "Windows listener must set SO_EXCLUSIVEADDRUSE",
            )
            self.assertFalse(
                self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR),
                "Windows listener must NOT set SO_REUSEADDR - it allows port hijacking",
            )
        else:
            # POSIX needs it, or a DCC port stays unusable through TIME_WAIT and
            # there are only eleven of them.
            self.assertTrue(
                self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR),
                "POSIX listener must set SO_REUSEADDR",
            )

    def test_a_prepared_listener_can_actually_bind_and_accept(self):
        platform_compat.prepare_listener(self.sock)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.assertGreater(self.sock.getsockname()[1], 0)


class KeepaliveTests(unittest.TestCase):
    def setUp(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(self.sock.close)

    def test_keepalive_is_enabled_on_every_platform(self):
        platform_compat.apply_keepalive(self.sock)
        self.assertTrue(self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))

    def test_linux_timing_knobs_are_applied_where_they_exist(self):
        platform_compat.apply_keepalive(self.sock, idle=7, interval=3, count=2)
        if hasattr(socket, "TCP_KEEPIDLE"):
            self.assertEqual(self.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE), 7)

    def test_missing_knobs_do_not_raise(self):
        """The whole point of the guard: no AttributeError on a platform without them."""
        platform_compat.apply_keepalive(self.sock)


class LongPathTests(unittest.TestCase):
    """Windows caps paths at 260 chars; a deep music library passes that easily."""

    def test_identity_on_posix(self):
        if platform_compat.IS_WINDOWS:
            self.skipTest("POSIX behaviour")
        p = "/srv/library/Artist/Album/Track.flac"
        self.assertEqual(platform_compat.long_path(p), p)

    def test_windows_gets_the_extended_prefix(self):
        if not platform_compat.IS_WINDOWS:
            self.skipTest("Windows behaviour")
        result = platform_compat.long_path("C:\\music\\Artist\\Track.flac")
        self.assertTrue(result.startswith("\\\\?\\"), result)

    def test_already_prefixed_is_left_alone(self):
        if not platform_compat.IS_WINDOWS:
            self.skipTest("Windows behaviour")
        already = "\\\\?\\C:\\music\\Track.flac"
        self.assertEqual(platform_compat.long_path(already), already)

    def test_unc_share_uses_the_unc_form(self):
        if not platform_compat.IS_WINDOWS:
            self.skipTest("Windows behaviour")
        result = platform_compat.long_path("\\\\nas\\music\\Track.flac")
        self.assertTrue(result.startswith("\\\\?\\UNC\\"), result)

    def test_empty_and_none_are_passed_through(self):
        self.assertEqual(platform_compat.long_path(""), "")
        self.assertIsNone(platform_compat.long_path(None))

    def test_a_deep_path_survives_a_round_trip_to_disk(self):
        """The behaviour that matters: open a file whose path is long."""
        root = tempfile.mkdtemp(prefix="dccore-long-")
        deep = root
        for i in range(12):
            deep = os.path.join(deep, "Artist Name With A Long Title %02d" % i)
        try:
            os.makedirs(platform_compat.long_path(deep), exist_ok=True)
        except OSError:
            self.skipTest("filesystem refused the deep path outright")
        target = os.path.join(deep, "12 - A Rather Long Classical Track Title.flac")
        with open(platform_compat.long_path(target), "wb") as handle:
            handle.write(b"audio")
        self.assertTrue(os.path.exists(platform_compat.long_path(target)))
        self.assertGreater(len(target), 260 if platform_compat.IS_WINDOWS else 0)


def wait_until(predicate, timeout=15.0, interval=0.01):
    """Poll until `predicate()` is true, or give up after `timeout`.

    Replaces a flat time.sleep() before asserting on work a background thread
    does. A fixed wait asserts on the CLOCK: it passes on a developer laptop
    where the thread finishes in a millisecond and fails on a loaded CI runner
    that has not been scheduled yet - reporting a defect that is not there.
    windows-latest/3.10 collected on a 0.4s wait here while 3.12 passed on the
    same image, which is how it announced itself.

    A generous ceiling rather than a tight one, because the thing being tested
    is that the work HAPPENS, not that it happens quickly. Something genuinely
    wedged still fails the run; it just takes 15 seconds to say so. Same
    correction as #152 and #157.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class MissingRarBinaryTests(unittest.TestCase):
    """A machine with no rar installed must degrade, not wedge.

    This is the most likely first failure on a fresh Windows box, and it is
    exactly the case CI exposed: the runners have no rar, so resolving the
    binary raises before subprocess.run is ever reached. Both process-wide
    interlocks must still be released, or folder packing is dead for every user
    until the daemon restarts.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))
        from tests.support import (reset_config, install_fake_oserve, silence_debug,
                                   no_disk_writes, RecordingSocket, TempTree)
        import announce, db, dcc
        self.config = reset_config()
        install_fake_oserve()
        silence_debug(announce)
        no_disk_writes(db)
        self.dcc = dcc
        self.sock = RecordingSocket()
        self.tree = TempTree()
        self.addCleanup(self.tree.cleanup)
        self.config.FILE_DIRECTORY = self.tree.music
        self.config.TMP_ZIP_DIR = os.path.join(self.tree.root, "tmp_zips")

        self._real = platform_compat.rar_command
        platform_compat.rar_command = lambda configured=None: None
        self.addCleanup(lambda: setattr(platform_compat, "rar_command", self._real))

    def test_missing_rar_releases_both_interlocks(self):
        row = {"file": "Album.rar", "path": self.tree.album, "channel": "#c",
               "user_raw": "dave", "is_unpacked_rar_folder": True, "is_temporary_zip": True}
        self.config.dcc_queue = {"dave": [row]}
        self.config.channel_users = {"#c": {"dave"}}
        self.config.bot_joined_channel = True

        self.dcc.check_queue_and_send(self.sock, "dave")
        wait_until(lambda: not self.config.rar_inprogress)

        self.assertFalse(self.config.rar_inprogress,
                         "a missing rar must not latch rar_inprogress - that kills packing "
                         "for every user until restart")
        self.assertNotIn("dave", getattr(self.config, "user_processing_lock", set()))

    def test_missing_rar_charges_the_row_rather_than_looping(self):
        row = {"file": "Album.rar", "path": self.tree.album, "channel": "#c",
               "user_raw": "dave", "is_unpacked_rar_folder": True, "is_temporary_zip": True}
        self.config.dcc_queue = {"dave": [row]}
        self.config.channel_users = {"#c": {"dave"}}
        self.config.bot_joined_channel = True

        self.dcc.check_queue_and_send(self.sock, "dave")
        wait_until(lambda: row.get("send_fails") is not None)

        self.assertEqual(row.get("send_fails"), 1,
                         "the failure must be charged to the retry budget, not retried forever")


class DescribeTests(unittest.TestCase):
    def test_describe_names_the_platform_and_rar_state(self):
        line = platform_compat.describe()
        self.assertIn("platform=", line)
        self.assertIn("python=", line)
        self.assertIn("rar=", line)


def _calls_to(module_name, dotted):
    """Every ast.Call node in `module_name` that calls `dotted`, e.g.
    "platform_compat.prepare_listener".

    A substring scan cannot tell a call from a mention. Four of the guards
    below were `assertIn("platform_compat.prepare_listener", source)`, which
    this very sentence would satisfy if it lived in the module.
    """
    owner, attribute = dotted.split(".")
    path = os.path.join(REPO_ROOT, module_name)
    with io.open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == attribute
                and isinstance(func.value, ast.Name) and func.value.id == owner):
            found.append(node)
    return found


class WiringTests(unittest.TestCase):
    """The daemon must actually use these rather than keeping its own copies.

    These were four substring scans of module source. The audit's objection is
    that `if False:` in front of a real call site leaves such a scan green, and
    a comment naming the function satisfies it with no call at all.

    Two things changed. The listener check below is now driven through the real
    send path, so it fails if the call stops happening. The rest are AST checks
    for a genuine Call node rather than text - weaker than execution, and said
    plainly rather than implied: an AST check still passes against `if False:`.
    They also cover all six call sites now. The substring versions looked at
    dcc.py and irc.py only, while adminchat.py (x2) and dcc_fetch.py (x1) make
    the same platform-specific calls and were never checked at all.
    """

    def test_dcc_calls_prepare_listener_on_the_real_send_path(self):
        """Executed, not scanned. prepare_listener() runs just before the bind
        loop in start_dcc_send(), so holding the only configured port open
        makes the send stop at "No available DCC ports" a few lines later -
        the call has already happened by then, and nothing waits on a
        30-second accept()."""
        import socket
        import dcc
        from tests.support import RecordingSocket, reset_config

        config = reset_config()
        calls = []
        real = platform_compat.prepare_listener

        def recorder(sock):
            calls.append(sock)
            return real(sock)

        platform_compat.prepare_listener = recorder
        self.addCleanup(setattr, platform_compat, "prepare_listener", real)

        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("0.0.0.0", 0))
        held.listen(1)
        self.addCleanup(held.close)
        busy = held.getsockname()[1]

        directory = tempfile.mkdtemp(prefix="dccore-wiring-")
        self.addCleanup(shutil.rmtree, directory, True)
        track = os.path.join(directory, "Song.flac")
        with io.open(track, "w", encoding="utf-8") as handle:
            handle.write("x" * 4096)

        config.DCC_PORT_START = busy
        config.DCC_PORT_END = busy
        # 8.8.8.8, not a TEST-NET address: Python classes 203.0.113.x as
        # private, so is_offerable_to_strangers() refuses it and the send
        # returns before it ever reaches the listener.
        config.MY_IP_OR_DOCK = "8.8.8.8"

        sock = RecordingSocket()
        dcc.start_dcc_send(sock, "dave", track, "Song.flac", "#chan",
                           {"file": "Song.flac", "path": track})

        self.assertTrue(calls,
                        "the listener socket was prepared some other way - on "
                        "Windows that means SO_REUSEADDR, which lets another "
                        "process take the incoming connection")

    def test_every_listener_is_prepared_through_platform_compat(self):
        for module in ("dcc.py", "adminchat.py", "dcc_fetch.py"):
            with self.subTest(module=module):
                self.assertTrue(
                    _calls_to(module, "platform_compat.prepare_listener"),
                    f"{module} binds a listener without preparing it")

    def test_no_module_sets_so_reuseaddr_itself(self):
        """The half that matters most on Windows, where SO_REUSEADDR means the
        OPPOSITE thing: it lets another process bind the same port and take the
        incoming connection, which on a DCC listener is a hijack."""
        for module in ("dcc.py", "adminchat.py", "dcc_fetch.py", "irc.py"):
            with self.subTest(module=module):
                with io.open(os.path.join(REPO_ROOT, module), encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                offenders = [node.lineno for node in ast.walk(tree)
                             if isinstance(node, ast.Attribute)
                             and node.attr == "SO_REUSEADDR"]

                self.assertEqual(offenders, [],
                                 f"{module} sets SO_REUSEADDR directly at line(s) "
                                 f"{offenders} - wrong on Windows")

    def test_dcc_resolves_the_rar_binary(self):
        self.assertTrue(_calls_to("dcc.py", "platform_compat.rar_command"))

    def test_nothing_hardcodes_the_bare_rar_name(self):
        """A bare "rar" is found on PATH on Linux and usually is not on
        Windows, where it lives under Program Files."""
        for module in ("dcc.py", "update_list.py"):
            with self.subTest(module=module):
                with io.open(os.path.join(REPO_ROOT, module), encoding="utf-8") as handle:
                    source = handle.read()

                self.assertNotIn('["rar", "a"', source)
                self.assertNotIn("['rar', 'a'", source)

    def test_keepalive_is_applied_through_platform_compat(self):
        for module in ("irc.py", "adminchat.py"):
            with self.subTest(module=module):
                self.assertTrue(
                    _calls_to(module, "platform_compat.apply_keepalive"),
                    f"{module} holds a long-lived socket open without keepalive")

    def test_the_call_scanner_can_tell_a_call_from_a_mention(self):
        """Control for _calls_to itself. The substring version of these guards
        would pass on a module whose only occurrence is inside a string."""
        import tempfile as tf

        with tf.NamedTemporaryFile("w", suffix=".py", delete=False,
                                   encoding="utf-8", dir=REPO_ROOT) as handle:
            handle.write('x = "platform_compat.prepare_listener"\n'
                         '# platform_compat.prepare_listener(sock)\n')
            path = handle.name
        self.addCleanup(os.remove, path)

        self.assertEqual(_calls_to(os.path.basename(path),
                                   "platform_compat.prepare_listener"), [])

    def test_config_supports_a_local_override(self):
        source = open(os.path.join(REPO_ROOT, "defaults.py"), encoding="utf-8").read()
        self.assertIn("from admin_config import *", source)

    def test_admin_config_is_gitignored(self):
        ignored = open(os.path.join(REPO_ROOT, ".gitignore"), encoding="utf-8").read()
        self.assertIn("admin_config.py", ignored)


if __name__ == "__main__":
    unittest.main()
