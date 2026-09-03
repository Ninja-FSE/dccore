"""Every thread the daemon starts is a daemon thread.

WHAT WAS WRONG

Twenty-five threads are started across the daemon. Twenty-three passed
daemon=True. Two did not, and they were the two most common things a user
types at this bot:

    irc.py  @<nick>            -> list.send_file_list
    irc.py  !<nick> <file>     -> dcc.handle_download_request

A non-daemon thread keeps the interpreter alive at exit. So a shutdown - Ctrl-C
in the terminal, a systemd stop, a container restart - waited on whatever those
two were doing, and they are the two that do the slowest work in the program:
DCC transfers to strangers on the internet.

Nothing was lost by making them daemons. The queue is persisted to
dcc_queue.txt and rescanned five seconds after the next start, which is the
whole point of the cold-start wake added in v1.9.0-RC1; a transfer cut short at
shutdown is one the bot picks up again. The other twenty-three threads, which
include an INCOMING file transfer (dcc_fetch.handle_incoming_offer), already
made that trade.

WHY THIS CHECK IS STATIC, DELIBERATELY

The last few PRs have been replacing source-text guards with tests that execute
the code, so a static check here needs its reason stated rather than assumed.

Reaching every one of these call sites by execution means driving twenty-five
separate IRC command paths, several of which open sockets. And it would still
be weaker: it would prove the sites a test happens to reach are correct, and
say nothing about the twenty-sixth added next month. Reading the call sites
proves the property for all of them at once.

The thing a source-text grep cannot do - tell a call from a mention, or see
through a rename - is exactly what the AST does here.
"""

import ast
import io
import os
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402


def thread_calls():
    """(module, line, is_daemon) for every threading.Thread(...) call."""
    out = []
    for name in sorted(os.listdir(REPO_ROOT)):
        if not name.endswith(".py"):
            continue
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            label = (func.attr if isinstance(func, ast.Attribute)
                     else getattr(func, "id", None))
            if label != "Thread":
                continue
            keywords = {kw.arg: kw for kw in node.keywords}
            daemon = keywords.get("daemon")
            is_daemon = (daemon is not None
                         and isinstance(daemon.value, ast.Constant)
                         and daemon.value.value is True)
            out.append((name, node.lineno, is_daemon))
    return out


class EveryStartedThreadIsADaemon(unittest.TestCase):

    def test_no_thread_is_left_non_daemon(self):
        offenders = [f"{name}:{line}" for name, line, ok in thread_calls() if not ok]

        self.assertEqual(
            offenders, [],
            "these threads keep the interpreter alive at exit, so a shutdown "
            "waits on them:\n  " + "\n  ".join(offenders))

    def test_the_scan_finds_the_threads(self):
        """Control. A scan that matched nothing would pass on any tree, which
        is the failure mode of every check shaped like this one."""
        found = thread_calls()

        self.assertGreater(len(found), 15,
                           "the daemon starts more threads than this; the scan "
                           "is not seeing them")

    def test_it_can_tell_a_daemon_from_a_non_daemon(self):
        """Control for the predicate rather than the scan. daemon=False and a
        missing daemon= are the same thing to the interpreter and must be the
        same thing here."""
        import tempfile

        source = ("import threading\n"
                  "threading.Thread(target=f).start()\n"
                  "threading.Thread(target=f, daemon=False).start()\n"
                  "threading.Thread(target=f, daemon=True).start()\n")
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8", dir=REPO_ROOT) as handle:
            handle.write(source)
            path = handle.name
        self.addCleanup(os.remove, path)

        mine = [(line, ok) for name, line, ok in thread_calls()
                if name == os.path.basename(path)]

        self.assertEqual(mine, [(2, False), (3, False), (4, True)])

    def test_the_two_command_handlers_are_covered_by_this(self):
        """Named because they are the ones that were wrong, and because a
        future reader should be able to find the case from the test."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        targets = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            label = (func.attr if isinstance(func, ast.Attribute)
                     else getattr(func, "id", None))
            if label != "Thread":
                continue
            for kw in node.keywords:
                if kw.arg == "target":
                    targets.append(ast.unparse(kw.value))

        self.assertIn("list.send_file_list", targets)
        self.assertIn("dcc.handle_download_request", targets)


class TheListenerIsClosedWhenNoPortIsFree(DCCoreTestCase):
    """start_dcc_send() creates its listening socket BEFORE it looks for a free
    port. Every path after the accept() runs through a finally: that closes it -
    but the "no free port" branch returns before reaching that try, so the
    socket was leaked.

    Once per refused request, on a path that schedules itself to retry in 45
    seconds. On a bot whose ports are genuinely exhausted that is a steady
    file-descriptor leak for exactly as long as the condition lasts, which is
    when the daemon can least afford one.
    """

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.track = os.path.join(self.tree.root, "Song.flac")
        with io.open(self.track, "w", encoding="utf-8") as handle:
            handle.write("x" * 4096)

        # Occupy the only port the send is allowed to use, so it reaches the
        # branch under test and stops there rather than blocking on a
        # 30-second accept(). Same technique as #193's control.
        self.held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.held.bind(("0.0.0.0", 0))
        self.held.listen(1)
        self.addCleanup(self.held.close)
        busy = self.held.getsockname()[1]
        self.set_config(DCC_PORT_START=busy, DCC_PORT_END=busy,
                        MY_IP_OR_DOCK="8.8.8.8")

        self.created = []
        real = socket.socket

        def recorder(*args, **kwargs):
            made = real(*args, **kwargs)
            self.created.append(made)
            return made

        socket.socket = recorder
        self.addCleanup(setattr, socket, "socket", real)

    def send(self):
        import dcc
        sock = RecordingSocket()
        dcc.start_dcc_send(sock, "dave", self.track, "Song.flac", "#chan",
                           {"file": "Song.flac", "path": self.track})
        return sock.text()

    def test_the_refusal_is_the_branch_being_exercised(self):
        """Control: if this stops saying "no available DCC ports", the test
        below is proving something about a different code path."""
        self.assertIn("no available dcc ports", self.send().lower())

    def test_the_listener_is_not_left_open(self):
        self.send()

        leaked = [s for s in self.created if s.fileno() != -1]
        for stray in leaked:
            stray.close()

        self.assertEqual(leaked, [],
                         "the listening socket was left open; one file "
                         "descriptor is lost per refused request")

    def test_it_is_still_leak_free_after_several_refusals(self):
        """The shape that matters. One leaked descriptor is a curiosity; the
        retry every 45 seconds is what turns it into an outage."""
        for _ in range(5):
            self.send()

        leaked = [s for s in self.created if s.fileno() != -1]
        for stray in leaked:
            stray.close()

        self.assertEqual(len(leaked), 0)


if __name__ == "__main__":
    unittest.main()
