"""Every raw write to the IRC socket takes the whole line (#504).

WHAT send() ACTUALLY DOES

`socket.send()` returns how many bytes it managed to hand to the kernel, and
the caller has to loop on the remainder. `sendall()` does that loop. Twenty
sites across five modules called `send()` and dropped the return value, so on
Linux - with the socket's kernel send buffer within a few hundred bytes of
full - the line goes out truncated, and the server reads whatever arrived as
a complete command.

#456 fixed `queue_mgr`'s two. This is the rest, and the reason to do them all
rather than the ones that look risky is that "this line is short, so it
cannot be truncated" is exactly the reasoning that left `queue_mgr` broken.
The buffer being full is a property of the CONNECTION, not of the line.

What it would look like when it happens: a truncated `PONG` is a ping timeout
and therefore a disconnect; a truncated `USER` is a failed registration; a
truncated DCC SEND handshake is a transfer that never starts with the slot
still held. None of those report themselves as a short write.

THE GUARD, AND WHY IT IS SHAPED LIKE THIS

A test that listed the twenty known sites would pass forever while somebody
added a twenty-first. So this asks the opposite question: is there ANY
`.send(` left in the daemon's own modules?

That works because this codebase has exactly one non-socket `send`:
`adminchat.Session.send()`, which writes a line to an admin's DCC CHAT
session through its own buffering. Every other `.send(` in a daemon module is
a socket. So the allowlist is three receiver names in one file, and anything
else is a finding rather than a judgement call.

NOT A RULE ABOUT WHICH PATH A LINE TAKES. Some of these bypass
`oserve.queue_message()` deliberately - the DCC negotiation lines do it for
latency, and `dcc.py` says so where it happens. `sendall()` is about whether
the whole line goes out, not about which road it takes to get there.
"""

import ast
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# adminchat.Session.send() is a line to an admin console, not a socket write.
# Named by RECEIVER and by file, so the exemption cannot quietly spread: a
# `session.send()` appearing in another module would be a new thing to look
# at rather than something this list waves through.
SESSION_SEND = {"adminchat.py": {"session", "self", "previous"}}


def daemon_modules():
    """Every module the daemon itself is made of.

    The repository root only - `scripts/` are operator tools that do not hold
    an IRC socket, and `tests/` stand-ins deliberately implement both `send`
    and `sendall` so that a test can prove which one production called.
    """
    for name in sorted(os.listdir(REPO_ROOT)):
        if name.endswith(".py") and not name.startswith("_"):
            yield name


def send_calls():
    """(module, line, receiver) for every `<something>.send(...)` call."""
    found = []
    for name in daemon_modules():
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "send":
                continue
            target = node.func.value
            receiver = getattr(target, "id", "") or getattr(target, "attr", "")
            found.append((name, node.lineno, receiver))
    return found


def sendall_calls():
    total = 0
    for name in daemon_modules():
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "sendall":
                total += 1
    return total


class NothingWritesToASocketWithSend(unittest.TestCase):

    def test_no_daemon_module_calls_send_on_a_socket(self):
        """The whole guard. Read from the AST rather than the text, so a
        `send(` inside a comment or a docstring - webserver.py has three,
        describing this very thing - is not a finding, and a call split
        across lines still is."""
        offenders = [(name, line, receiver) for name, line, receiver in send_calls()
                     if receiver not in SESSION_SEND.get(name, set())]

        self.assertEqual(offenders, [],
                         "send() returns how many bytes it took and the caller "
                         "must loop on the rest; use sendall(). If one of these "
                         "is genuinely not a socket, add its receiver to "
                         "SESSION_SEND with a reason.")

    def test_the_allowlist_still_describes_something_real(self):
        """The other half. An allowlist for a name nothing uses any more is a
        hole that looks like a rule - and if Session.send() were renamed, this
        test would be the only thing still claiming it exists."""
        for name, receivers in SESSION_SEND.items():
            seen = {receiver for module, _line, receiver in send_calls()
                    if module == name}
            for receiver in receivers:
                with self.subTest(module=name, receiver=receiver):
                    self.assertIn(receiver, seen,
                                  "nothing calls %s.send() in %s any more - "
                                  "remove it from the allowlist rather than "
                                  "leaving an exemption nothing needs"
                                  % (receiver, name))

    def test_the_session_exemption_is_a_method_and_not_a_socket(self):
        """Named rather than assumed. `session.send` is exempt because
        adminchat.Session defines it, and that is a fact about the class, not
        about the spelling of a variable."""
        import adminchat

        self.assertTrue(callable(getattr(adminchat.Session, "send", None)),
                        "Session.send() has gone, so the exemption in this "
                        "file is now excusing something else")

    def test_the_sites_were_actually_converted_not_deleted(self):
        """A coarse backstop, and described as one.

        The guard above is satisfied by deleting every socket write, which
        would be the worst possible way to pass it. What actually owns "the
        line is still sent" is the rest of the suite, and it does own it:
        replacing the USER registration line with `pass` fails six tests in
        other files, none of them this one. This only catches a wholesale
        removal, which is the shape that would otherwise slip past both.
        """
        self.assertGreaterEqual(sendall_calls(), 20,
                                "the socket writes are gone rather than "
                                "converted")


class WhatGoesOnTheWire(unittest.TestCase):
    """Two properties of the converted lines themselves."""

    def test_every_converted_call_still_sends_bytes(self):
        """`sendall()` takes bytes. A str reaching it is a TypeError on
        whichever thread is sending - the reader thread, for a PONG."""
        import ast as _ast

        for name in daemon_modules():
            with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                tree = _ast.parse(handle.read(), filename=name)
            for node in _ast.walk(tree):
                if not (isinstance(node, _ast.Call)
                        and isinstance(node.func, _ast.Attribute)
                        and node.func.attr == "sendall"):
                    continue
                if not node.args:
                    continue
                argument = node.args[0]
                with self.subTest(module=name, line=node.lineno):
                    self.assertFalse(
                        isinstance(argument, _ast.Constant)
                        and isinstance(argument.value, str),
                        "a str literal is being handed to sendall()")

    def test_an_unspellable_name_costs_a_character_not_an_exception(self):
        """A filename read off disk can carry a surrogate that utf-8 cannot
        encode. Raising on the send would take down whichever thread is
        holding the socket, so every site that encodes a str does it the way
        announce.py's drain always has."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename="dcc.py")

        encodes = 0
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "sendall"):
                continue
            for argument in node.args:
                if isinstance(argument, ast.Call) \
                        and isinstance(argument.func, ast.Attribute) \
                        and argument.func.attr == "encode":
                    encodes += 1
                    with self.subTest(line=node.lineno):
                        self.assertTrue(
                            argument.args or argument.keywords,
                            "encode() with no arguments raises on a name the "
                            "socket cannot spell")

        self.assertGreater(encodes, 0, "no encoded sendall found in dcc.py")
