"""The dashboard's console (#1322): the log of what the bot printed, the
sink that collects it, and running a console command as if it had been
typed at the DCC console.

Split out of webserver.py, step 6 of splitting it (docs/FUTURE.md). The
routes are still there and call these through webserver, which imports
them back - except _console_next_id and _console_sink_registered, which
this module reassigns: a copy on webserver would keep its import-time
value. A test that sets them sets them here.
"""

import collections
import threading
import time

import adminchat


# ---------------------------------------------------------------------
# CONSOLE
#
# A web-native alternative to the DCC CHAT admin console (adminchat.py) and
# the debug channel, for an operator who wants neither: no second IRC client
# to keep open, no channel broadcasting the daemon's internals to whoever
# joins it.
#
# Two halves, kept separate on purpose:
#   - the LOG is the ambient stream every announce.send_debug() call already
#     produces. Registering below as another consumer of the same fan-out
#     adminchat.py's own DCC sessions use (announce.add_debug_sink) - nothing
#     downstream of send_debug() has to learn a new destination exists, and
#     DEBUG_TO_CONSOLE (already on by default, independent of DEBUG_TO_CHANNEL
#     / DEBUG_CHANNEL) is the only switch that gates it - see send_debug()'s
#     own "ROUTING" comment. An operator who wants no debug channel at all
#     still gets this for free.
#   - a COMMAND is request/response: POST one line, get back exactly the
#     lines that one command produced, not interleaved with the ambient log.
#
# adminchat.COMMANDS is reused directly rather than re-implemented: a second
# copy of "queue", "status", "rehash" and the rest would drift the moment one
# of them changed - the same shape as every drift this codebase has already
# been bitten by (PRESERVE_RUNTIME, the two check-setup.py copies).
#
# announce.py is reloaded on every !rehash (it is in commands.CORE_MODULES),
# which resets announce._debug_sinks to [] - but commands.py's rehash handler
# already snapshots every live sink before that reload and reattaches the
# same objects after (see reattach_debug_sinks()), generically, regardless of
# which module registered them. webserver.py is NOT itself in CORE_MODULES -
# it is never reloaded - so _console_log and _console_next_id need none of
# the "survive my own reload" handling announce._debug_queue uses for itself.
# ---------------------------------------------------------------------

_console_log = collections.deque(maxlen=500)


_console_log_lock = threading.Lock()


_console_next_id = 1


_console_sink_registered = False


def _console_debug_sink(msg_text, category="INFO"):
    """announce.add_debug_sink's callback shape: (plain text, category).

    Must not block and must not raise - announce.py's own comment on the sink
    contract says why: send_debug() is called from the IRC read thread. A
    plain append under a short lock, the same shape as
    adminchat.Session.send().

    strip_irc_formatting(), not a fresh copy of it: some send_debug() callers
    embed mIRC control characters of their own inside msg_text for the
    channel's benefit, and adminchat.Session.debug_sink() already strips them
    for the identical reason - a console is read as a log, not rendered by an
    IRC client.
    """
    global _console_next_id
    with _console_log_lock:
        _console_log.append({
            "id": _console_next_id,
            "category": str(category),
            "time": time.time(),
            "text": adminchat.strip_irc_formatting(msg_text),
        })
        _console_next_id += 1


def _ensure_console_sink():
    """Register the console's debug sink, at most once.

    Safe to call from more than one place - the log route and start() both
    do, since a test that calls build_console_log_payload() directly never
    runs start() - because announce.add_debug_sink() is itself idempotent
    (a no-op when the sink is already present). The module-level flag just
    avoids importing announce on every poll.
    """
    global _console_sink_registered
    if _console_sink_registered:
        return
    import announce
    announce.add_debug_sink(_console_debug_sink)
    _console_sink_registered = True


def build_console_log_payload(since=0):
    """Every console log line newer than `since`, plus the cursor to poll
    from next.

    A cursor rather than an offset/limit pair: this is a live stream a client
    polls repeatedly, and an offset would either replay lines already shown
    or, worse, silently skip ones that arrived between two polls. since=0 -
    the first poll - returns whatever is currently buffered (up to 500
    lines): a newly opened console showing recent history is strictly more
    useful than one that starts blank, and costs nothing extra since the
    buffer already exists.
    """
    _ensure_console_sink()
    try:
        since = int(since)
    except (TypeError, ValueError):
        since = 0
    with _console_log_lock:
        lines = [line for line in _console_log if line["id"] > since]
        cursor = _console_next_id - 1
    return {"lines": lines, "cursor": cursor}


# Commands that make no sense over a stateless HTTP request. "quit" closes a
# DCC CHAT session (session.close()) - _WebConsoleSession below has no socket
# to close and no persistent identity for that to mean anything about.
#
# "hello" switches a session to the structured feed (#550): it sets
# session.structured and pushes STATUS lines to a client that draws a window
# from them. A one-shot HTTP request has no feed to switch, and the dashboard's
# own Console is prose. It used to fail halfway - after printing the DCCORE
# HELLO line - on an attribute the shim did not have (#581).
_CONSOLE_UNSUPPORTED_COMMANDS = frozenset({"quit", "hello", "downloads"})


_CONSOLE_UNSUPPORTED_MESSAGES = {
    "quit": "'quit' closes a DCC CHAT session; there is not one here. Just close this tab.",
    "hello": "'hello' switches a DCC CHAT session to the structured feed for a script "
             "such as dccore.mrc; this console is the dashboard's own and has no feed to switch.",
    "downloads": "'downloads' tells a DCC CHAT session that its Downloads window is open; this console "
                 "has none. The Downloads page is the dashboard's own.",
}


class _WebConsoleSession:
    """Just enough of adminchat.Session's shape for COMMANDS' handlers, which
    is only ever .send() plus the two attributes handle_command() itself
    touches (.nick, .last_activity) - checked directly against adminchat.py,
    not guessed at. Never a real Session: no socket, no writer thread,
    nothing to close.
    """

    # What the handlers read besides .send(): pair asks .structured to choose
    # between a DCCORE TOKEN line and prose (#581). A web request is always
    # prose - it used to be missing, so `pair` wrote the new token to disk and
    # then raised before showing it.
    structured = False
    draws_dlqueue = False
    # `library` subscribes a structured session to its lines (#1359); this
    # one is never structured, so it reads the prose and never asks a burst.
    reads_library = False
    client = "web"
    # Behind the dashboard login, which is the admin password itself - so
    # unlocked, like a console that logged in with the password (#1264).
    unlocked = True

    def __init__(self, nick):
        self.nick = nick
        self.last_activity = time.time()
        self.lines = []

    def send(self, text=""):
        self.lines.append(str(text))

    def request_status(self):
        """No status burst here: a web request has no feed."""


def build_console_command_result(command_text, remote_addr=None):
    """Run one admin command through adminchat.handle_command() and return
    exactly the lines it produced.

    Reuses the real command set rather than a second copy of it - see the
    CONSOLE section's own comment above for why.

    ASYNC COMMANDS RETURN AN ACKNOWLEDGEMENT, NOT THE RESULT. ban, unban,
    clearqueue, rehash and update each run the real work on a background
    thread (adminchat._run_detached) and reply immediately with only a
    "Banning ..." / "Rehashing ..." line - the same as a DCC CHAT session
    sees. Their eventual result is not lost: the handlers underneath all call
    announce.send_debug() when they finish, which reaches this same page
    through the LOG half above, a few seconds later. A caller of this
    function only ever gets what handle_command() produced synchronously.
    """
    # ASCII whitespace only, as adminchat.handle_command() strips (#1264
    # review): a settings value may end in a no-break space.
    stripped = str(command_text or "").strip(" \t\r\n")
    if not stripped:
        return 400, {"error": "No command given."}

    name = stripped.split(None, 1)[0].lower()
    if name in _CONSOLE_UNSUPPORTED_COMMANDS:
        return 200, {"lines": [_CONSOLE_UNSUPPORTED_MESSAGES.get(
            name, f"'{name}' is not available in this console.")]}

    session = _WebConsoleSession(f"web:{remote_addr or 'unknown'}")
    adminchat.handle_command(session, stripped)
    return 200, {"lines": session.lines}
