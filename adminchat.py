# adminchat.py - Authenticated DCC CHAT console for the operator.
"""The operator's console: transport, authentication, and the commands.

This file is the whole admin console. It authenticates a session, and it
defines and dispatches every command that session can run - including the
destructive ones (ban, unban, clearqueue, rehash, update). COMMANDS at the
bottom is the complete list.

It said the opposite until #231: "Phase 1: transport and authentication only.
The admin commands themselves are NOT here yet, deliberately." That was true
when the file was written and stopped being true when the dispatcher landed.
It is the first thing anyone reads here, and it claimed a narrow
security-review scope - "who may open a session, and what proves it" - for a
file that also holds every command worth reviewing carefully.

The authentication is still the part to read first, and it is described below.
But it is no longer the only thing here.

WHY DCC CHAT RATHER THAN A CHANNEL OR PM COMMAND
------------------------------------------------
is_admin() compares a nick against ADMIN_NICK, and on Undernet a nick is not
owned without services auth - anyone can take the admin nick while the operator
is offline and inherit every admin command, including !clearqueue.

The gate here is the operator's Undernet services login. When a user logs into X
and sets usermode +x, the server replaces their host with
"<account>.users.undernet.org". Only the server can issue that host, and only to
someone holding that account, so matching the host IS verifying the login - with
no password shared with this bot and nothing to steal from its config.

The socket is then the session. There is no token, no nick binding and no expiry
bookkeeping: it dies when the TCP connection dies. That is the part a PM-based
!auth command can never get right, because it always ends up trusting a nick
again once the password has been accepted.

WHERE THIS DELIBERATELY DIFFERS FROM iroffer
---------------------------------------------
iroffer screens only the remote IP at connect time; its hostmask test lives in
dcc_host_password() and runs together with the password. So iroffer answers a
stranger: it accepts, prints a banner naming its version, build, OS, feature list
and uptime, and prompts.

This one screens the host on the incoming CTCP, before replying at all. A stranger
gets no banner, no connection, no reply of any kind, and no way to learn whether
the mask was wrong. The cost of an unauthorised attempt is one regex.

CONNECTION DIRECTION, AND THE INBOUND SURFACE
---------------------------------------------
The preferred path follows iroffer's non-passive form: the requesting client
listens and supplies its ip/port, and the bot connects OUT to it. That path
opens no listening port.

Both fallbacks do. _open_chat_listener() binds a port in the
DCC_PORT_START..DCC_PORT_END range when the bot cannot dial the operator's
client, and the passive form - the client offering port 0, meaning "you listen
instead" - is parsed and answered with an offer of our own. So this module is
not inbound-surface-free, and the host check is what stands in front of that
surface.

Two earlier claims here were wrong and are corrected rather than deleted,
because both were load-bearing for anyone deciding how much of this file needs
a security review: that no listening port is opened (#217), and that passive
DCC is not supported (#231). parse_offer() has handled port 0 since the
listen-mode work, and tests/test_adminchat.py drives it end to end.
"""

import binascii
import collections
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import threading
import time

import defaults as config
import platform_compat

# --------------------------------------------------------------------------
# Tunables. Deliberately module constants rather than config entries: these are
# safety limits, not preferences, and an operator lowering them by accident
# would weaken the gate.
# --------------------------------------------------------------------------
CONNECT_TIMEOUT = 10.0        # dialling the operator's client
LISTEN_TIMEOUT = 60.0         # waiting for the operator to accept our offer back
SEND_TIMEOUT = 30.0           # a blocked write gives up rather than hanging forever
AUTH_TIMEOUT = 60.0           # seconds to supply a password before the socket closes
IDLE_TIMEOUT = 1800.0         # authenticated session, so a forgotten window expires
MAX_PASSWORD_ATTEMPTS = 3
WRONG_PASSWORD_DELAY = 1.0    # slows scripted guessing without tying up the reader
BAD_IP_BLOCK_SECONDS = 900.0
OUTBOX_MAX = 500              # bounded: a stalled client drops lines, never grows

PBKDF2_ITERATIONS = 200_000

# --------------------------------------------------------------------------
# Module state. This module is deliberately absent from commands.py's
# CORE_MODULES: importlib.reload re-executes a module body, which would
# drop a live session's socket on the floor on every !rehash. That is not
# hypothetical - it is what used to happen to every runtime container in
# config.py until PRESERVE_RUNTIME was added.
# --------------------------------------------------------------------------
_session = None               # the one authenticated session, or None
_pending = None               # at most one connected-but-unauthenticated session
_state_lock = threading.Lock()

_bad_ips = {}                 # ip -> [failure_count, blocked_until]
_bad_lock = threading.Lock()


# ==========================================================================
# Hostmask matching
# ==========================================================================

def source_host(prefix_or_line):
    """The host half of an IRC prefix, lowercased, or None.

    Accepts either a bare prefix ("nick!ident@host") or a whole raw line, so
    callers do not have to slice it first.
    """
    if not prefix_or_line:
        return None
    text = str(prefix_or_line)
    if text.startswith(":"):
        text = text[1:]
    text = text.split(" ", 1)[0]
    if "@" not in text:
        return None
    return text.rsplit("@", 1)[1].strip().lower() or None


def host_pattern_of(mask):
    """Reduce a configured mask to the HOST pattern it really means.

    A mask may be written either as a bare host ("operator.users.undernet.org") or in
    the familiar iroffer/IRC form ("*!*@operator.users.undernet.org"). Either way only
    the part after the last "@" is used.

    The nick and ident halves are discarded ON PURPOSE. In nick!ident@host the
    ident is supplied by the client - anyone can set theirs to "operator" - so a
    pattern that appears to constrain it grants no security while breaking the
    moment the operator's client changes its ident setting. Only the host is
    issued by the server.
    """
    if not mask:
        return None
    text = str(mask).strip().lower()
    if not text:
        return None
    if "@" in text:
        text = text.rsplit("@", 1)[1].strip()
    return text or None


def _compile(pattern):
    """Wildcard pattern to anchored regex, matching security.py's hard-ban idiom."""
    return re.compile("^" + re.escape(pattern).replace(r"\*", ".*") + "$")


def admin_host_patterns():
    """Configured host patterns, ignoring blanks. Empty means the console is off."""
    raw = getattr(config, "ADMIN_HOSTMASKS", None) or []
    if isinstance(raw, str):
        raw = [part for part in raw.split(",")]
    patterns = []
    for entry in raw:
        pattern = host_pattern_of(entry)
        if pattern and pattern not in patterns:
            patterns.append(pattern)
    return patterns


def is_admin_host(prefix_or_line):
    """True only when the line's HOST matches a configured admin pattern.

    A pattern reducing to nothing once wildcards are stripped is refused: it
    would admit every host on the network and make the whole gate decorative.
    security.py refuses an all-wildcard hard ban for the mirror-image reason.

    Strips "*!@." - the same four characters security.py's own hard-ban guard
    strips, not just "*". A HOST cannot contain "!" or "@" (only a full
    <nick>!<ident>@<host> hostmask can), so those two are no-ops here - but a
    host is made of dot-separated labels, and "*.*" reduces to a lone "." under
    a stars-only strip: truthy, so it passed and compiled to a pattern
    matching essentially every real host. #218, found by the same audit that
    caught the hard-ban version of this in #168.
    """
    host = source_host(prefix_or_line)
    if not host:
        return False
    for pattern in admin_host_patterns():
        residue = pattern
        for separator in "*!@.":
            residue = residue.replace(separator, "")
        if not residue:
            print(f"[ADMINCHAT] Refusing dangerously broad ADMIN_HOSTMASKS entry: {pattern!r}")
            continue
        if _compile(pattern).match(host):
            return True
    return False


# ==========================================================================
# Password
# ==========================================================================

def make_password_hash(password, iterations=PBKDF2_ITERATIONS):
    """Build the value to paste into admin_config.ADMIN_PASSWORD_HASH.

    pbkdf2_hmac rather than scrypt: scrypt is the stronger primitive, but it
    depends on the OpenSSL build Python was linked against and raises where that
    is missing. pbkdf2 is always present in the standard library on every
    platform, which matters for a daemon that has to run on both Linux and
    Windows. The threat model tolerates it - an attacker must already hold the
    operator's Undernet services account before the password is even reachable.
    """
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, binascii.hexlify(salt).decode(), binascii.hexlify(digest).decode())


def verify_password(stored, supplied):
    """Constant-time check of `supplied` against a stored pbkdf2 string.

    Returns False rather than raising on a malformed or empty stored value: a
    console with no password configured must refuse everyone, not admit them.
    """
    if not stored or not supplied:
        return False
    try:
        scheme, iterations, salt_hex, digest_hex = str(stored).split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = binascii.unhexlify(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", str(supplied).encode("utf-8"),
            binascii.unhexlify(salt_hex), int(iterations))
    except (ValueError, binascii.Error, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


def password_is_configured():
    return bool(getattr(config, "ADMIN_PASSWORD_HASH", ""))


# ==========================================================================
# Bad-IP tracking
# ==========================================================================
# Copied from iroffer's count_badip()/is_in_badip(). Counting attempts within one
# session is useless on its own, because an attacker simply reconnects; the count
# has to outlive the connection.

def note_bad_ip(ip):
    if not ip:
        return
    with _bad_lock:
        entry = _bad_ips.get(ip) or [0, 0.0]
        entry[0] += 1
        if entry[0] >= MAX_PASSWORD_ATTEMPTS:
            entry[1] = time.time() + BAD_IP_BLOCK_SECONDS
            print(f"[ADMINCHAT] {ip} blocked for {int(BAD_IP_BLOCK_SECONDS)}s "
                  f"after {entry[0]} failed password attempt(s).")
        _bad_ips[ip] = entry


def is_bad_ip(ip):
    if not ip:
        return False
    with _bad_lock:
        entry = _bad_ips.get(ip)
        if not entry:
            return False
        if entry[1] and time.time() >= entry[1]:
            del _bad_ips[ip]      # block expired; forget it entirely so a typo is not permanent
            return False
        return bool(entry[1])


def clear_bad_ip(ip):
    """A successful login clears the record for that address."""
    with _bad_lock:
        _bad_ips.pop(ip, None)


# ==========================================================================
# Session
# ==========================================================================

_IRC_FORMATTING = re.compile("\\x03\\d{0,2}(?:,\\d{1,2})?|[\\x02\\x0f\\x16\\x1d\\x1f]")


def strip_irc_formatting(text):
    """Drop mIRC colour and attribute codes.

    The debug channel's lines are built for a colour-capable client sitting in a
    channel. A console is read as a log; the codes only get in the way, and some
    clients render a DCC CHAT window without colour support at all.
    """
    return _IRC_FORMATTING.sub("", str(text))


class Session:
    """One DCC CHAT connection. The socket IS the session.

    Writes never happen on a caller's thread. Everything that wants to say
    something appends to a bounded deque and a dedicated writer thread drains it.
    That is not tidiness: send_debug() is called from the IRC read loop, and if a
    log line could block on a stalled admin client - a minimised window, a sleeping
    laptop, a half-open TCP connection - the daemon's network thread would freeze
    and drop off the server. The same bounded hand-off announce.py already uses.
    """

    def __init__(self, sock, peer_ip, nick, host):
        self.sock = sock
        self.peer_ip = peer_ip
        self.nick = nick
        self.host = host
        self.authenticated = False
        self.opened_at = time.time()
        self.last_activity = time.time()
        self.attempts = 0
        self.closed = False
        self.dropped = 0
        self._outbox = collections.deque(maxlen=OUTBOX_MAX)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._writer = None

    # -- output ------------------------------------------------------------

    def send(self, text=""):
        """Queue one line. Never blocks, never raises, never touches the socket."""
        if self.closed:
            return
        if len(self._outbox) == self._outbox.maxlen:
            self.dropped += 1
        self._outbox.append(str(text))
        self._wake.set()

    def start_writer(self):
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()

    def _writer_loop(self):
        while not self.closed:
            if not self._outbox:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            try:
                line = self._outbox.popleft()
            except IndexError:
                continue
            # DCC CHAT is line-oriented and terminated with \n. mIRC accepts \r\n
            # too, but a bare \n is what every other client expects.
            payload = (line + "\n").encode("utf-8", "replace")
            try:
                with self._lock:
                    self.sock.sendall(payload)
            except (OSError, socket.timeout) as err:
                print(f"[ADMINCHAT] Write to {self.nick} failed ({err}); closing session.")
                self.close(announce_text=None)
                return

    def debug_sink(self, msg_text, category="INFO"):
        """Target for announce.send_debug's fan-out.

        A pure append, like send() itself. This runs on whatever thread called
        send_debug - including the IRC read loop - so it must never touch the
        socket or wait for anything.
        """
        if self.closed or not self.authenticated:
            return
        self.send(f"[{category}] {strip_irc_formatting(msg_text)}")

    # -- lifecycle ---------------------------------------------------------

    def close(self, announce_text="Session closed."):
        if self.closed:
            return
        try:
            import announce
            announce.remove_debug_sink(self.debug_sink)
        except Exception:
            pass
        if announce_text:
            # Written inline rather than queued: the writer thread is about to
            # stop, so a queued goodbye would never leave the building.
            try:
                with self._lock:
                    self.sock.sendall((announce_text + "\n").encode("utf-8", "replace"))
            except OSError:
                pass
        self.closed = True
        self._wake.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def expired(self, now=None):
        """True when this session has outstayed its allowance."""
        now = now if now is not None else time.time()
        if not self.authenticated:
            return (now - self.opened_at) > AUTH_TIMEOUT
        return (now - self.last_activity) > IDLE_TIMEOUT


# ==========================================================================
# Banner and command surface
# ==========================================================================

def banner_lines():
    """Modelled on iroffer's chat_banner(): welcome, build, then the prompt.

    The uptime line iroffer prints is deliberately absent from the BANNER, but
    the value behind it is live: _uptime_seconds() below wraps
    stats_mgr.get_uptime_seconds(), and both `uptime` and `status` report it.

    This used to say the function was "called from nowhere" and reset to zero on
    every !rehash. Neither is true any more: it has two callers, and stats_mgr
    guards start_time with a try/except NameError precisely so a reload leaves
    the original value standing (#232).
    """
    return [
        "",
        f"Welcome to {getattr(config, 'NICKNAME', None)}",
        f"{getattr(config, 'SCRIPT_VERSION', '')} - {platform_compat.describe()}",
        "",
    ]


def format_uptime(seconds):
    """iroffer's phrasing, because the banner is modelled on iroffer's."""
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} Day{'s' if days != 1 else ''}")
    if hours or days:
        parts.append(f"{hours} Hr{'s' if hours != 1 else ''}")
    parts.append(f"{minutes} Min")
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0]


def _uptime_seconds():
    import stats_mgr
    return stats_mgr.get_uptime_seconds()


# --------------------------------------------------------------------------
# Read-only commands. These build their own output, so they answer directly.
# --------------------------------------------------------------------------

def _cmd_version(session, args):
    session.send(getattr(config, "SCRIPT_VERSION", ""))
    session.send(platform_compat.describe())


def _cmd_uptime(session, args):
    session.send(f"Running {format_uptime(_uptime_seconds())}")


def _cmd_slots(session, args):
    transfers = list(getattr(config, "active_transfers", []))
    limit = getattr(config, "MAX_DCC_SLOTS", 0)
    session.send(f"Slots: {len(transfers)}/{limit} in use")
    if not transfers:
        session.send("  (nothing sending)")
        return
    for tx in transfers:
        sent = tx.get("bytes_sent", 0)
        session.send(f"  {str(tx.get('user', '?')):<20} {str(tx.get('file', '?'))[:48]:<48} "
                     f"{sent:,} bytes")


def _cmd_queue(session, args):
    queue = dict(getattr(config, "dcc_queue", {}))
    frozen = dict(getattr(config, "frozen_queues", {}))
    target = args.strip().lower()

    if target:
        rows = queue.get(target, [])
        if not rows:
            session.send(f"{target} has nothing queued.")
            return
        session.send(f"{target}: {len(rows)} file(s)"
                     f"{' (FROZEN)' if target in frozen else ''}")
        for row in rows:
            name = row.get("file", "?") if isinstance(row, dict) else str(row)
            session.send(f"  {name}")
        return

    if not queue:
        session.send("The queue is empty.")
        return
    total = sum(len(rows) for rows in queue.values())
    session.send(f"{total} file(s) queued for {len(queue)} user(s):")
    for user_key in sorted(queue):
        session.send(f"  {user_key:<20} {len(queue[user_key]):>4} file(s)"
                     f"{'  FROZEN' if user_key in frozen else ''}")


def _cmd_status(session, args):
    import dcc
    import list as list_mod

    transfers = getattr(config, "active_transfers", [])
    session.send(f"{getattr(config, 'SCRIPT_VERSION', '')} - "
                 f"running {format_uptime(_uptime_seconds())}")
    session.send(f"Nick        : {getattr(config, 'NICKNAME', '?')}")
    session.send(f"Slots       : {len(transfers)}/{getattr(config, 'MAX_DCC_SLOTS', 0)} in use")
    try:
        session.send(f"Queued      : {dcc.get_total_queued_count()} file(s) for "
                     f"{len(getattr(config, 'dcc_queue', {}))} user(s)")
    except Exception as err:
        session.send(f"Queued      : unavailable ({err})")
    session.send(f"Frozen      : {len(getattr(config, 'frozen_queues', {}))} queue(s)")
    session.send(f"Timed bans  : {len(getattr(config, 'banned_users', {}))}")
    try:
        import db
        session.send(f"Hard bans   : {len(db.load_hard_bans())}")
    except Exception as err:
        session.send(f"Hard bans   : unavailable ({err})")
    try:
        count, date_str, size_str, _ = list_mod.get_file_count_date_size_and_raw_bytes()
        session.send(f"MasterList  : {count:,} files, {size_str}, dated {date_str}")
    except Exception as err:
        session.send(f"MasterList  : unavailable ({err})")


# --------------------------------------------------------------------------
# Action commands.
#
# Each one runs on its own thread. rehash reloads eight modules and update walks
# the whole NFS library, which takes minutes - doing either on the reader thread
# would freeze this session for the duration and let its idle timeout fire.
#
# They report through announce.send_debug, which reaches this console via the
# sink registered at authentication, so their output arrives here as well as in
# the debug channel.
#
# authorised=True: the session has already proved the operator's services login
# and a password, which is a stronger claim than the nick comparison inside
# those handlers - and that comparison would refuse a console whose current nick
# is not in ADMIN_NICK.
# --------------------------------------------------------------------------

def _run_detached(session, label, fn):
    def wrapper():
        try:
            fn()
        except Exception as err:
            session.send(f"{label} failed: {err}")
            print(f"[ADMINCHAT] {label} raised: {err}")
    threading.Thread(target=wrapper, daemon=True).start()


def _cmd_ban(session, args):
    pattern = args.strip()
    if not pattern:
        session.send("Usage: ban <pattern>   e.g. ban *!*@spammer.net")
        return
    import commands
    session.send(f"Banning {pattern} ...")
    _run_detached(session, "ban", lambda: commands.handle_hard_ban_request(
        session.nick, CONSOLE_SOURCE, f"!ban {pattern}", authorised=True))


def _cmd_unban(session, args):
    pattern = args.strip()
    if not pattern:
        session.send("Usage: unban <pattern>")
        return
    import commands
    session.send(f"Unbanning {pattern} ...")
    _run_detached(session, "unban", lambda: commands.handle_hard_unban_request(
        session.nick, CONSOLE_SOURCE, f"!unban {pattern}", authorised=True))


def _cmd_bans(session, args):
    import db
    patterns = db.load_hard_bans()
    timed = dict(getattr(config, "banned_users", {}))
    if not patterns and not timed:
        session.send("No bans.")
        return
    if patterns:
        session.send(f"Permanent ({len(patterns)}):")
        for pattern in patterns:
            session.send(f"  {pattern}")
    if timed:
        session.send(f"Timed ({len(timed)}):")
        for user_key in sorted(timed):
            session.send(f"  {user_key}")


def _cmd_clearqueue(session, args):
    target = args.strip()
    if not target:
        session.send("Usage: clearqueue <nick>")
        return
    import commands
    session.send(f"Clearing the queue for {target} ...")
    _run_detached(session, "clearqueue", lambda: commands.handle_admin_clear_queue(
        session.nick, CONSOLE_SOURCE, f"!clearqueue {target}", authorised=True))


def _cmd_rehash(session, args):
    import commands
    session.send("Rehashing - reloading modules in place ...")
    _run_detached(session, "rehash", lambda: commands.handle_rehash_request(
        session.nick, CONSOLE_SOURCE, authorised=True))


def _cmd_update(session, args):
    import commands
    session.send("Rebuilding the MasterList - this walks the whole library and "
                 "can take minutes ...")
    _run_detached(session, "update", lambda: commands.handle_list_update_request(
        session.nick, CONSOLE_SOURCE, authorised=True))


def _cmd_quit(session, args):
    session.close(announce_text="Goodbye.")
    _forget(session)


def _cmd_verify(session, args):
    """Report filenames the master list carries under more than one folder.

    Reads the list rather than walking the library: it is the list a requester
    copies a name out of, and the list dcc.py resolves that name against, so
    it is where a collision actually matters.
    """
    import list as list_mod

    entries, _total = list_mod.find_matching_entries([], limit=None)
    if not entries:
        session.send("The master list is empty or could not be read. "
                     "Run 'update' first.")
        return

    duplicates = list_mod.find_duplicate_filenames(entries)
    if not duplicates:
        session.send(f"{len(entries):,} file(s) checked - every filename is unique.")
        return

    session.send(f"{len(duplicates):,} filename(s) appear under more than one "
                 f"folder, out of {len(entries):,} checked.")
    session.send("A request that names a file alone reaches only the first copy "
                 "listed under that name. Pasting a search result's whole line, "
                 "size included, reaches the copy that size names:")
    for item in duplicates[:VERIFY_CONSOLE_LIMIT]:
        session.send(f"  {item['filename']}  ({item['count']} folders)")
        for folder in item["folders"]:
            session.send(f"      {list_mod.resolve_list_folder(folder)}")
    if len(duplicates) > VERIFY_CONSOLE_LIMIT:
        session.send(f"  ... and {len(duplicates) - VERIFY_CONSOLE_LIMIT:,} more "
                     f"not shown. The dashboard's Tools view lists them all.")


def _cmd_help(session, args):
    session.send("Available commands:")
    for name in sorted(COMMANDS):
        _fn, summary, usage = COMMANDS[name]
        session.send(f"  {usage:<20} {summary}")
    session.send("")
    session.send("Channel commands still work as before; this console does not "
                 "replace them yet.")


# How many duplicate names the console spells out in full. Every line here
# is a DCC CHAT line, so a library with hundreds of collisions would flood
# the session; the count is always reported, only the detail is capped.
VERIFY_CONSOLE_LIMIT = 20


# name -> (function, one-line summary, usage shown by help)
COMMANDS = {
    "status":     (_cmd_status,     "everything at a glance",            "status"),
    "queue":      (_cmd_queue,      "queued files, all or one user",     "queue [nick]"),
    "slots":      (_cmd_slots,      "what is sending right now",         "slots"),
    "bans":       (_cmd_bans,       "permanent and timed bans",          "bans"),
    "uptime":     (_cmd_uptime,     "how long the daemon has run",       "uptime"),
    "version":    (_cmd_version,    "build and platform",                "version"),
    "ban":        (_cmd_ban,        "add a permanent wildcard ban",      "ban <pattern>"),
    "unban":      (_cmd_unban,      "remove a permanent wildcard ban",   "unban <pattern>"),
    "clearqueue": (_cmd_clearqueue, "force-clear another user's queue",  "clearqueue <nick>"),
    "rehash":     (_cmd_rehash,     "reload modules in place",           "rehash"),
    "update":     (_cmd_update,     "rebuild the MasterList",            "update"),
    "verify":     (_cmd_verify,     "filenames listed in two folders",   "verify"),
    "help":       (_cmd_help,       "this list",                         "help"),
    "quit":       (_cmd_quit,       "close this session",                "quit"),
}

# What the admin handlers see as the "channel" a console command came from. It
# is not a channel, and it must not look like one: announce.send_debug puts this
# straight into the debug line, and a real channel name there would read as
# though someone had typed the command in public.
CONSOLE_SOURCE = "DCC-CONSOLE"


def handle_command(session, text):
    """Dispatch one authenticated line."""
    stripped = text.strip()
    if not stripped:
        return
    command, _, args = stripped.partition(" ")
    entry = COMMANDS.get(command.lower())
    if entry is None:
        session.send(f"Unknown command: {command}. Type 'help'.")
        return
    session.last_activity = time.time()
    print(f"[ADMINCHAT] {session.nick} ran: {stripped}")
    try:
        entry[0](session, args)
    except Exception as err:
        # One bad command must not take the session down with it.
        session.send(f"Command failed: {err}")
        print(f"[ADMINCHAT] Command {command!r} raised: {err}")


# ==========================================================================
# Reader loop
# ==========================================================================

def _forget(session):
    global _session, _pending
    with _state_lock:
        if _session is session:
            _session = None
        if _pending is session:
            _pending = None


def _promote(session):
    """A newly authenticated session replaces any live one.

    Replace rather than refuse, because the realistic case is the operator's own
    stale window: a session left open on another machine, or a client that froze
    while the server has not yet timed the nick out. Refusing would lock him out
    until the old TCP connection died on its own.

    The replacement happens only AFTER the new session authenticates, never on
    connect. Otherwise anyone who matched the host could drop the operator's live
    console without knowing the password.
    """
    global _session, _pending
    with _state_lock:
        previous = _session
        _session = session
        if _pending is session:
            _pending = None
    # Runtime reports reach the console from here on. Registered only after
    # authentication, so an unauthenticated socket never sees a log line.
    try:
        import announce
        announce.add_debug_sink(session.debug_sink)
    except Exception as sink_err:
        print(f"[ADMINCHAT] Could not attach the debug sink: {sink_err}")

    if previous is not None and previous is not session:
        previous.send(f"Session taken over from {session.peer_ip}. Closing this one.")
        previous.close(announce_text=None)
    return previous


def _reader_loop(session):
    buffer = ""
    try:
        while not session.closed:
            if session.expired():
                reason = ("No password within %ds." % int(AUTH_TIMEOUT)
                          if not session.authenticated
                          else "Idle for %d minutes." % int(IDLE_TIMEOUT / 60))
                session.close(announce_text=reason)
                break
            try:
                data = session.sock.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break

            session.last_activity = time.time()
            buffer += data.decode("utf-8", "replace")
            # Clients disagree about the terminator; normalise before splitting.
            buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if session.closed:
                    break
                if session.authenticated:
                    handle_command(session, line)
                else:
                    _check_password(session, line)
            # A client that never sends a newline must not grow the buffer forever.
            if len(buffer) > 4096:
                session.close(announce_text="Line too long.")
                break
    finally:
        session.close(announce_text=None)
        _forget(session)


def _check_password(session, line):
    supplied = line.strip()
    if not supplied:
        return
    if verify_password(getattr(config, "ADMIN_PASSWORD_HASH", ""), supplied):
        session.authenticated = True
        session.last_activity = time.time()
        clear_bad_ip(session.peer_ip)
        replaced = _promote(session)
        session.send("")
        session.send("Entering DCC Chat Admin Interface")
        session.send('For help type "help"')
        if replaced is not None:
            session.send(f"(replaced an existing session from {replaced.peer_ip})")
        session.send("")
        print(f"[ADMINCHAT] {session.nick} authenticated from {session.host} ({session.peer_ip}).")
        return

    session.attempts += 1
    note_bad_ip(session.peer_ip)
    print(f"[ADMINCHAT] Failed password from {session.nick} ({session.host}), "
          f"attempt {session.attempts}/{MAX_PASSWORD_ATTEMPTS}.")
    if session.attempts >= MAX_PASSWORD_ATTEMPTS:
        session.close(announce_text="Incorrect Password.")
        _forget(session)
        return
    # Small fixed delay. NOT exponential: this runs on the session's own reader
    # thread, and a long sleep would just hold the thread open for an attacker.
    time.sleep(WRONG_PASSWORD_DELAY)
    session.send("Incorrect Password.")
    session.send("Enter Your Password:")


# ==========================================================================
# Entry point from irc.py
# ==========================================================================

def parse_offer(ctcp_text):
    """Pull a dialable (ip, port) out of 'DCC CHAT chat <ip-as-long> <port>'.

    Returns None for anything malformed. Otherwise returns (ip, port), where ip
    is None when the client's offer cannot be dialled and the bot should listen
    instead:

    * port 0 is the passive form - the client is explicitly asking us to listen.
    * 0.0.0.0 means the client does not know its own address. mIRC sends this
      when its Local Info lookup has not resolved, and it is not a no-op: on
      Linux connect() to 0.0.0.0 is treated as "this host", so the bot dials
      ITSELF and gets ECONNREFUSED. That is exactly the
      "Could not connect to operator at 0.0.0.0:11283" in the field report.
    * multicast and reserved ranges cannot be a listening client either.

    Loopback and private addresses are deliberately kept dialable: an operator
    on the same LAN as the daemon, or testing locally, is a legitimate case.
    """
    parts = str(ctcp_text).strip().strip("\x01").split()
    if len(parts) < 4 or parts[0].upper() != "DCC" or parts[1].upper() != "CHAT":
        return None

    # Read fields by POSITION, never from the end. The two forms differ in length:
    #
    #   active   DCC CHAT chat <ip> <port>            5 tokens
    #   passive  DCC CHAT chat <ip> 0 <token>         6 tokens
    #
    # Counting back from the end works only for the active form. On a passive
    # offer parts[-2] is the literal 0 and parts[-1] is the token, so
    # "DCC CHAT chat 3405803861 0 350" parsed as 0.0.0.0 port 350 and was thrown
    # out as unusable - which is exactly what the field report showed.
    #
    # The third token is the "chat" argument, but not every client sends one, so
    # it is skipped only when it is not itself a number.
    rest = parts[2:]
    if rest and not rest[0].lstrip("-").isdigit():
        rest = rest[1:]
    if len(rest) < 2:
        return None

    try:
        ip_long = int(rest[0])
        port = int(rest[1])
    except (ValueError, TypeError):
        return None
    token = rest[2] if len(rest) > 2 else None

    if port < 0 or port > 65535:
        return None
    if ip_long < 0 or ip_long > 0xFFFFFFFF:
        return None
    if port == 0:
        # Passive DCC. The token identifies this request and MUST come back in
        # our own offer, or the client cannot match the two and ignores us.
        return None, 0, token
    if port < 1024:
        return None
    try:
        address = ipaddress.IPv4Address(ip_long)
    except (ipaddress.AddressValueError, ValueError):
        return None
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        return None, port, token
    return str(address), port, token


def handle_dcc_chat(irc_sock, line, nick, ctcp_text):
    """Entry point. Returns True if the request was taken up.

    Called from the IRC read loop, so it must not block: every path either
    returns immediately or hands off to a thread.
    """
    if not is_admin_host(line):
        # Silence. No reply, no NOTICE, no debug line - a debug line to the
        # channel would tell anyone watching that the mask was wrong, and would
        # let a stranger fill the log for free. stdout only.
        print(f"[ADMINCHAT] Ignored DCC CHAT from unauthorised host: {source_host(line)}")
        return False

    if not password_is_configured():
        print("[ADMINCHAT] DCC CHAT from an authorised host refused: "
              "ADMIN_PASSWORD_HASH is not set. Generate one with "
              "adminchat.make_password_hash() and put it in admin_config.py.")
        return False

    offer = parse_offer(ctcp_text)
    if offer is None:
        print(f"[ADMINCHAT] Unusable DCC CHAT offer from {nick}: {ctcp_text!r}")
        return False

    ip, port, token = offer
    host = source_host(line)

    if ip is None:
        # Either passive DCC, or a client that does not know its own address and
        # sent 0.0.0.0. Listen and offer the connection back instead of dialling
        # somewhere that cannot answer.
        print(f"[ADMINCHAT] {nick} offered no usable address "
              f"({'passive DCC' if port == 0 else 'unroutable IP'}); listening instead.")
        threading.Thread(target=_listen_and_serve, args=(irc_sock, nick, host, token),
                         daemon=True).start()
        return True

    if is_bad_ip(ip):
        print(f"[ADMINCHAT] DCC CHAT from {ip} refused: address is temporarily blocked.")
        return False

    mode = str(getattr(config, "ADMIN_CHAT_MODE", "auto") or "auto").strip().lower()
    if mode == "listen":
        # The operator knows their client is not reachable and does not want to
        # pay CONNECT_TIMEOUT discovering it again on every single login.
        print(f"[ADMINCHAT] ADMIN_CHAT_MODE is 'listen'; offering the connection to {nick} "
              f"rather than dialling {ip}:{port}.")
        threading.Thread(target=_listen_and_serve, args=(irc_sock, nick, host, token),
                         daemon=True).start()
        return True

    threading.Thread(target=_connect_and_serve,
                     args=(irc_sock, nick, host, ip, port, token), daemon=True).start()
    return True


def _open_chat_listener():
    """Bind a listener inside the configured DCC port range.

    Scans DOWNWARD from DCC_PORT_END. start_dcc_send() scans upward from
    DCC_PORT_START, so a console opening while transfers are running tends to
    land at the far end rather than competing for the same port. With
    MAX_DCC_SLOTS transfers plus one console there is room either way.

    The range is deliberately the same one as DCC SEND: it is already forwarded
    to the daemon, so the console needs no new firewall rule.
    """
    start = int(getattr(config, "DCC_PORT_START", 55000))
    end = int(getattr(config, "DCC_PORT_END", 55010))
    for port in range(end, start - 1, -1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR means the OPPOSITE thing on Windows - it lets another
        # process bind the same port and take the connection. platform_compat
        # picks the right option per platform.
        platform_compat.prepare_listener(sock)
        try:
            sock.bind(("0.0.0.0", port))
            # listen() HERE, not at the call site. On POSIX, SO_REUSEADDR lets a
            # second socket bind the same port while the first is bound but not
            # yet listening - so returning a merely-bound socket let two console
            # attempts claim one port and fight over the connection. Listening
            # claims it properly, and bind then fails for the second caller.
            # Windows never had the hole, because SO_EXCLUSIVEADDRUSE refuses the
            # duplicate at bind; CI caught it on Linux only.
            sock.listen(1)
            return sock, port
        except OSError:
            sock.close()
            continue
    return None, None


def _serve(sock, peer_ip, nick, host, description):
    """Banner, prompt and reader loop. Shared by both transports."""
    global _pending

    # A short recv timeout keeps the reader loop responsive enough to notice its
    # own auth/idle deadlines; the send timeout stops a stalled peer wedging the
    # writer thread forever.
    sock.settimeout(1.0)
    platform_compat.apply_keepalive(sock, idle=60, interval=15, count=4)

    session = Session(sock, peer_ip, nick, host)

    with _state_lock:
        stale_pending = _pending
        _pending = session
    if stale_pending is not None:
        # Only one unauthenticated socket at a time, so a matching host cannot
        # pile up half-open connections.
        stale_pending.close(announce_text="Superseded by a newer connection.")

    session.start_writer()
    for text in banner_lines():
        session.send(text)
    session.send("Enter Your Password:")

    print(f"[ADMINCHAT] DCC CHAT {description} for {nick} ({host}).")
    _reader_loop(session)
    print(f"[ADMINCHAT] Session with {nick} closed.")


def _connect_and_serve(irc_sock, nick, host, ip, port, token=None):
    """Dial the operator's listening client, and listen instead if that fails.

    Dialling out is the tidier path - one dialog rather than two - but it can
    only work if the client is actually reachable at the address it advertised,
    and there are several ordinary reasons it is not:

      * the client is behind a VPN, so it reports the VPN's exit address while
        inbound connections to it are not forwarded anywhere;
      * a router is not forwarding the port, or a firewall drops rather than
        rejects, which shows up as a TIMEOUT rather than a refusal;
      * the daemon's own outbound to high ports is blocked.

    A failure here used to be the end of it. It is not: the bot's own listener
    is proven reachable every day by DCC SEND, so falling back to offering the
    connection costs one timeout and then works. Set ADMIN_CHAT_MODE = "listen"
    to skip straight to it and not pay the timeout at all.
    """
    try:
        sock = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
    except OSError as err:
        mode = str(getattr(config, "ADMIN_CHAT_MODE", "auto") or "auto").strip().lower()
        if mode == "connect":
            print(f"[ADMINCHAT] Could not connect to {nick} at {ip}:{port} ({err}). "
                  f"ADMIN_CHAT_MODE is 'connect', so not falling back to listening.")
            return
        print(f"[ADMINCHAT] Could not connect to {nick} at {ip}:{port} ({err}); "
              f"falling back to listening. Set ADMIN_CHAT_MODE = \"listen\" in "
              f"admin_config.py to go straight here and skip the wait.")
        _listen_and_serve(irc_sock, nick, host, token)
        return
    _serve(sock, ip, nick, host, f"opened to {ip}:{port}")


def _listen_and_serve(irc_sock, nick, host, token=None):
    """Listen on the configured range and offer the connection back.

    Used when the client's own offer cannot be dialled - it asked for passive
    DCC, or it does not know its own address and sent 0.0.0.0. This is iroffer's
    chat_setup_out() path, and it is the more robust one here: it depends on
    nothing the client knows about itself, and it uses the port range the
    operator already forwards for DCC SEND.

    The address advertised is the bot's own public IP, resolved once at startup
    and already used for every DCC SEND handshake.
    """
    import dcc

    ip_long = dcc.get_public_ip_long()
    if not ip_long:
        print("[ADMINCHAT] Cannot offer a DCC CHAT: the bot's own public IP is unknown "
              "(config.MY_IP_OR_DOCK did not resolve).")
        return

    listener, port = _open_chat_listener()
    if listener is None:
        start = getattr(config, "DCC_PORT_START", 55000)
        end = getattr(config, "DCC_PORT_END", 55010)
        print(f"[ADMINCHAT] No free port in {start}-{end} for the console; "
              f"all of them are in use by transfers.")
        return

    sock = None
    try:
        # Already listening - _open_chat_listener does it, so the port is claimed
        # before this function ever advertises it.
        listener.settimeout(LISTEN_TIMEOUT)
        # A passive request carries a token identifying it, and the reply must
        # carry the same one back or the client cannot match our offer to the
        # request it is waiting on, and silently ignores us.
        suffix = f" {token}" if token else ""
        offer = f"PRIVMSG {nick} :\x01DCC CHAT chat {ip_long} {port}{suffix}\x01\r\n"
        irc_sock.send(offer.encode())
        print(f"[ADMINCHAT] Offered DCC CHAT to {nick} on "
              f"{getattr(config, 'MY_IP_OR_DOCK', '?')}:{port}; waiting for the connection.")
        sock, addr = listener.accept()
        peer_ip = addr[0]
    except socket.timeout:
        print(f"[ADMINCHAT] {nick} did not accept the DCC CHAT offer within "
              f"{int(LISTEN_TIMEOUT)}s; giving the port back.")
        return
    except OSError as err:
        print(f"[ADMINCHAT] Could not offer a DCC CHAT to {nick} ({err}).")
        return
    finally:
        try:
            listener.close()
        except OSError:
            pass

    # The peer address is only known now, so the blocklist is checked here rather
    # than before the offer, as it is on the dial-out path.
    if is_bad_ip(peer_ip):
        print(f"[ADMINCHAT] Connection from {peer_ip} dropped: address is temporarily blocked.")
        try:
            sock.close()
        except OSError:
            pass
        return

    _serve(sock, peer_ip, nick, host, f"accepted from {peer_ip} on port {port}")


# ==========================================================================
# Introspection, for phase 3 and for tests
# ==========================================================================

def active_session():
    """The live authenticated session, or None."""
    with _state_lock:
        session = _session
    if session is not None and session.closed:
        return None
    return session


def reset_state_for_tests():
    """Drop all sessions and bad-IP records. Tests only."""
    global _session, _pending
    with _state_lock:
        sessions = [s for s in (_session, _pending) if s is not None]
        _session = None
        _pending = None
    for session in sessions:
        session.close(announce_text=None)
    with _bad_lock:
        _bad_ips.clear()


def _read_password(prompt):
    """Read a password without echoing it, falling back when there is no console.

    getpass on Windows reads the console device directly and ignores redirected
    stdin, so piping into this script hangs forever rather than failing. Detect
    that and read stdin instead - echoed, and said so out loud.
    """
    import getpass
    import sys as _sys
    if _sys.stdin is not None and _sys.stdin.isatty():
        return getpass.getpass(prompt)
    print(prompt + "(input is not a terminal, so it will be echoed)")
    line = _sys.stdin.readline()
    if not line:
        raise SystemExit("No input.")
    return line.rstrip("\r\n")


if __name__ == "__main__":
    print("Generate the value for admin_config.ADMIN_PASSWORD_HASH.")
    first = _read_password("Password: ")
    second = _read_password("Again: ")
    if first != second:
        raise SystemExit("Passwords did not match.")
    if not first:
        raise SystemExit("Empty password refused.")
    print()
    print('ADMIN_PASSWORD_HASH = "%s"' % make_password_hash(first))
    print()
    print("Paste that line into admin_config.py (gitignored), not config.py.")
