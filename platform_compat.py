# =====================================================================
# PLATFORM_COMPAT.PY - the few places where Linux and Windows differ
# =====================================================================
# The daemon is otherwise plain Python and runs unchanged on both. This module
# exists so the handful of genuine differences live in ONE place instead of
# being scattered through dcc.py and irc.py as platform checks.
#
# Every function here is a no-op or an identity on Linux, so nothing about the
# production behaviour changes. The point is that a Windows build stops needing
# a fork of the shared code.
# ---------------------------------------------------------------------

import os
import shutil
import socket
import sys
import threading
import time

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------
# The rar binary
# ---------------------------------------------------------------------
def rar_command(configured=None):
    """Return the rar executable to invoke, or None if it cannot be found.

    dcc.py used the bare string "rar", which relies on it being on PATH under
    that exact name. That holds on the Linux container and does not on Windows,
    where WinRAR installs rar.exe outside PATH entirely.

    A configured absolute path always wins, so an operator can point at a
    specific build without touching code.
    """
    if configured:
        if os.path.isfile(configured):
            return configured
        found = shutil.which(configured)
        if found:
            return found

    for candidate in ("rar", "rar.exe"):
        found = shutil.which(candidate)
        if found:
            return found

    if IS_WINDOWS:
        # WinRAR does not add itself to PATH, so look where it actually installs.
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if not base:
                continue
            candidate = os.path.join(base, "WinRAR", "rar.exe")
            if os.path.isfile(candidate):
                return candidate

    return None


# ---------------------------------------------------------------------
# Listening sockets
# ---------------------------------------------------------------------
def prepare_listener(sock):
    """Apply the correct address-reuse option for this platform.

    These two flags have the SAME NAME and OPPOSITE MEANINGS:

      POSIX   SO_REUSEADDR lets a new listener bind a port still in TIME_WAIT
              from a previous connection. Without it, a DCC port stays
              unusable for a couple of minutes after every transfer, which
              matters here because there are only eleven of them.

      Windows SO_REUSEADDR lets a DIFFERENT PROCESS bind a port this one is
              already listening on, and it may then receive the connection.
              On a DCC listener that is a hijack: the leecher's client
              connects and gets somebody else's socket. The Windows option
              with the POSIX meaning is SO_EXCLUSIVEADDRUSE, which explicitly
              forbids that.

    dcc.py set SO_REUSEADDR unconditionally, so a Windows build would have
    shipped that hole.
    """
    if IS_WINDOWS:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return sock

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return sock


def apply_keepalive(sock, idle=10, interval=2, count=3):
    """Turn on TCP keepalive, with the tuning knobs where they exist.

    SO_KEEPALIVE is portable. The three timing options are Linux-specific, so
    they stay guarded - on Windows the system defaults apply, which are slower
    but still catch a dead link.
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
    return sock


# ---------------------------------------------------------------------
# Console encoding
# ---------------------------------------------------------------------
def install_console_encoding_guard(streams=None):
    """Stop a limited console code page from killing the daemon.

    The log strings in this project contain Swedish characters, and print()
    encodes with whatever code page the attached stream happens to use. That
    is fine on a Western European box, where cp1252 contains a-ring, a-umlaut
    and o-umlaut - and fatal anywhere else:

        cp1252  (Western European)   encodes them      no symptom
        cp1253  (Greek)              cannot            UnicodeEncodeError
        cp1251  (Cyrillic)           cannot            UnicodeEncodeError
        cp932   (Japanese)           cannot            UnicodeEncodeError
        ascii   (POSIX/C locale)     cannot            UnicodeEncodeError

    An uncaught UnicodeEncodeError from a print() kills whatever thread ran
    it. list.py prints on every completed search, so on a Greek-locale box the
    search thread dies the first time anyone searches.

    It only bites when the stream is NOT an interactive console: PEP 528 makes
    Python talk UTF-16 to a real console window regardless of code page, so a
    developer running the daemon in a terminal sees nothing wrong. Redirect
    that same command to a log file, or run it as a service, and every print()
    with a non-ASCII character becomes a crash. That is exactly how this
    daemon is meant to run.

    Three settings, because each closes a different gap:

      encoding="utf-8"      so a redirected log keeps the real characters
      errors="replace"      so ANY stream that still cannot encode something
                            degrades to "?" instead of raising
      line_buffering=True   so a line that has been printed is ON DISK

    That last one is not about encoding, but it belongs at the same moment and
    on the same streams. Python block-buffers stdout whenever it is not a
    console - a pipe, a log file, a service host - so print() output sits in a
    4-8KB buffer instead of reaching the file. Measured while deploying this
    daemon: 75 seconds of startup logging produced ONE line on disk, and
    force-killing the process lost every buffered line, including the JOIN and
    the channel advert. A probe that kills a child mid-run recovers 0 of ~24
    printed lines without this and 23 of 24 with it.

    The lines lost that way are the ones leading up to whatever killed the
    process, which are the only lines anyone actually needs. The cost is a
    flush per line, which is nothing against this daemon's log volume.

    The real fix for the ENCODING half is for the log strings to be English -
    that half only guarantees a character can never take the process down
    while the translation happens.

    Returns the list of stream names actually reconfigured, so startup can say
    so and the tests can assert on it.
    """
    if streams is None:
        streams = (("stdout", sys.stdout), ("stderr", sys.stderr))

    changed = []
    for name, stream in streams:
        # pythonw.exe gives None for both, and a test harness may swap in an
        # object with no reconfigure() at all. Neither is an error.
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue

        current = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        try:
            if current == "utf8":
                # Already lossless. Pin the error handler so a later
                # reconfigure elsewhere cannot reintroduce the crash, and the
                # buffering, which is wrong regardless of the encoding.
                reconfigure(errors="replace", line_buffering=True)
            else:
                reconfigure(encoding="utf-8", errors="replace",
                            line_buffering=True)
        except (ValueError, OSError, AttributeError):
            # A detached or already-closed stream. Nothing to protect.
            continue
        changed.append(name)

    return changed


# ---------------------------------------------------------------------
# Console timestamps
# ---------------------------------------------------------------------
# Lives beside install_console_encoding_guard() because it is installed at the
# same moment, on the same two streams, and the two have to agree about what a
# stream is: the guard reconfigure()s the real TextIOWrapper in place and this
# wraps it, so a later reconfigure() must still reach the real one. Not a
# platform difference - a console-hygiene one, kept with its sibling.

class _TimestampedStream:
    """A stream proxy that prefixes every LINE with the time it was written.

    Prefixes lines, not writes. print() hands the stream its text and its
    newline in separate calls, and a multi-line message - a traceback, a
    folder listing - arrives as one write holding several lines. So the proxy
    tracks whether the last character it saw ended a line, and stamps the
    start of every line that begins on this stream, however the text was
    split up to reach it.

    Everything else - encoding, isatty(), fileno(), reconfigure(), flush() -
    is delegated to the real stream untouched, so code that inspects
    sys.stdout finds what it always found. In particular the encoding guard's
    reconfigure() lands on the real wrapper, and a later reconfigure() by
    anyone else does too.

    The format is read on every line rather than captured at install, so the
    setting can change after the wrapper is in place - the daemon installs
    this before config has loaded (so the config-loading lines are stamped
    too) and applies the operator's format a moment later.
    """

    def __init__(self, stream, formatter):
        self._stream = stream
        self._formatter = formatter
        self._at_line_start = True
        self._lock = threading.Lock()

    def write(self, text):
        if not isinstance(text, str):
            # A real text-mode stream raises here too - even for b"", which
            # is exactly the input click.utils._is_binary_writer() probes
            # with to decide whether a stream takes bytes or str. Returning
            # 0 for b"" (its old behaviour under `if not text: return 0`,
            # true for b"" as much as for "") answered that probe wrong:
            # click concluded this proxy was a binary stream, wrapped it in
            # its own encoder, and fed every real write here as encoded
            # bytes instead of text - which is what took the dashboard down
            # the moment Flask's CLI banner used click.echo() to print it.
            raise TypeError(
                f"write() argument must be str, not {type(text).__name__}")
        if not text:
            return 0
        fmt = self._formatter()
        if not fmt:
            return self._stream.write(text)
        with self._lock:
            out = []
            stamp = None
            for piece in text.splitlines(keepends=True):
                if self._at_line_start:
                    if stamp is None:
                        stamp = "[" + time.strftime(fmt) + "] "
                    out.append(stamp)
                out.append(piece)
                self._at_line_start = piece.endswith(("\n", "\r"))
            written = self._stream.write("".join(out))
        # Report what the CALLER wrote, not what reached the stream. A caller
        # comparing the return value to len(text) must not be told its write
        # was longer than the text it gave.
        return len(text) if written else 0

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._stream, name)

    # Cooperate with anyone who unwraps: the real stream is one attribute
    # away, and the guard's own tests look at it.
    @property
    def wrapped(self):
        return self._stream


_console_timestamp_format = ""


def console_timestamp_format():
    """The strftime format currently stamped on every console line, or ""."""
    return _console_timestamp_format


def set_console_timestamp_format(fmt):
    """Change the format for every already-installed wrapper. "" turns it off.

    Validated by formatting once: a typo like "%Q" would otherwise surface as a
    ValueError from inside every print() for the life of the process, which is
    exactly the kind of failure the encoding guard exists to prevent. An
    invalid format is refused and the previous one kept.

    "Invalid" means whatever THIS platform's strftime raises on, because that
    is where the hazard is. Windows' C runtime raises ValueError on an unknown
    directive; glibc passes it through as literal text, which is harmless and
    nothing to refuse. Refusing by a directive whitelist instead would reject
    locale and platform directives that work perfectly well.
    """
    global _console_timestamp_format
    fmt = str(fmt or "")
    if fmt:
        try:
            time.strftime(fmt)
        except (ValueError, TypeError) as err:
            print(f"[CONSOLE] CONSOLE_TIMESTAMP_FORMAT {fmt!r} is not a valid "
                  f"strftime format ({err}); keeping "
                  f"{_console_timestamp_format!r}.")
            return _console_timestamp_format
    _console_timestamp_format = fmt
    return fmt


def install_console_timestamps(fmt="%H:%M:%S"):
    """Prefix every line printed to stdout and stderr with the time.

    A log line with no time on it answers "what" and never "when", and the
    daemon's console is a log: when it rejoined a channel, how long a rebuild
    took, whether the disconnect came before or after the transfer. An
    operator reading the window during a live problem was working that out
    from their own memory of when they looked.

    Idempotent - a stream already wrapped is left alone, so installing twice
    does not double-stamp. Returns the names of the streams wrapped now, the
    same contract as install_console_encoding_guard(), so startup can say so
    and the tests can assert on it.

    Only the DAEMON installs this. scripts/setup_check.py and configure.py
    print reports for a person to read once, not logs, and a stamp on every
    line of a report is noise.
    """
    set_console_timestamp_format(fmt)
    changed = []
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        # pythonw.exe gives None for both; a stream already wrapped is left
        # alone; anything without write() is not a stream we can prefix.
        if stream is None or isinstance(stream, _TimestampedStream):
            continue
        if not hasattr(stream, "write"):
            continue
        setattr(sys, name, _TimestampedStream(stream, console_timestamp_format))
        changed.append(name)
    return changed


# ---------------------------------------------------------------------
# Long paths
# ---------------------------------------------------------------------
def long_path(path):
    """Make a path usable past the Windows 260-character MAX_PATH limit.

    Identity on Linux. On Windows, prefixing an ABSOLUTE path with \\\\?\\ opts
    into the extended-length API.

    This is not theoretical for a music library:

        Artist\\Album Name (Year)\\CD2\\12 - A Long Classical Track Title.flac

    nests past 260 characters without trying, and the failure is an
    unhelpful FileNotFoundError on a file that is plainly there.
    """
    if not IS_WINDOWS or not path:
        return path

    text = str(path)
    if text.startswith("\\\\?\\"):
        return text

    absolute = os.path.abspath(text)
    if absolute.startswith("\\\\"):
        # UNC share: \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + absolute.lstrip("\\")
    return "\\\\?\\" + absolute


def describe():
    """One line for the startup log, so the platform in use is never a guess."""
    rar = rar_command(getattr(sys.modules.get("defaults"), "RAR_BINARY", None))
    return (
        f"platform={'windows' if IS_WINDOWS else 'posix'} "
        f"python={sys.version_info.major}.{sys.version_info.minor} "
        f"rar={rar or 'NOT FOUND'}"
    )


# ---------------------------------------------------------------------
# ONE DAEMON PER DATA FOLDER (#710, audit L46). Nothing checked for an
# already-running instance: a logon task plus a manual double-click (or a
# second logon session of the same account) ran two bots on one data folder
# - the second took ALT_NICKNAME, both wrote dcc_queue.txt and stats.txt
# whole, and their DCC listeners shared the eleven-port range. The guard is
# an OS lock on a file, not the file's existence: a lock dies with its
# process, so a crash or a power cut leaves nothing stale behind for the
# next start to be refused by. msvcrt on Windows, fcntl elsewhere - both
# stdlib, both advisory-exclusive, both released the moment the handle
# closes or the process ends.
# ---------------------------------------------------------------------

class AlreadyRunning(RuntimeError):
    """Another DCCore holds the lock. `pid` is what it wrote, or None."""

    def __init__(self, path, pid=None):
        super().__init__(f"another DCCore is already running on this folder ({path})")
        self.path = path
        self.pid = pid


_instance_lock = {"handle": None, "path": None}
# Windows locks a byte RANGE, and a locked byte cannot be read by anyone -
# the holder included - so the pid at the start of the file would be
# unreadable if the lock sat there. It sits well past the pid instead; a
# range past the end of the file is a legal thing to lock on Windows.
_LOCK_OFFSET = 1024


def _pid_in(path):
    """The pid the holder wrote at the start of the lock file, or None. A
    raw read of the first bytes only: a buffered text read would take a
    chunk that includes the locked byte and be refused on Windows."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        return int(os.read(fd, 64).decode("ascii", "replace").strip() or "0") or None
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def take_instance_lock(path):
    """Hold `path` for this process's life, or raise AlreadyRunning.

    Idempotent within one process (a second call returns the same handle),
    so a test that boots twice is not its own second instance. The pid is
    written into the file for the refusal's message; the LOCK is what
    guards, the pid is only what is said.
    """
    if _instance_lock["handle"] is not None and _instance_lock["path"] == os.path.abspath(path):
        return _instance_lock["handle"]
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AlreadyRunning(os.path.abspath(path), _pid_in(path))
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        pass  # the lock is held either way; the pid is only for the message
    _instance_lock["handle"] = handle
    _instance_lock["path"] = os.path.abspath(path)
    return handle


def release_instance_lock():
    """Let the lock go (tests; the daemon holds it until it exits)."""
    handle = _instance_lock["handle"]
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass
    _instance_lock["handle"] = None
    _instance_lock["path"] = None


def replace_with_retry(src, dst, attempts=5, base_delay=0.02):
    """os.replace(), retrying a bounded number of times with backoff on
    PermissionError.

    #162 finding #25: on Windows, os.replace() raises PermissionError
    ([WinError 5]) when another handle has `dst` open at the exact instant of
    the rename - security.check_user_status() does exactly that, holding
    hard_bans.txt open (unlocked, no share-deny) on the IRC read thread for
    every PRIVMSG. Measured under synthetic load: 256/300 replace attempts
    failed with a reader active throughout. A bounded retry-with-backoff
    (total worst case here: ~0.3s across 4 sleeps) gives that brief per-line
    read window time to close without leaving a bad-actor open handle able to
    block a write indefinitely - this still raises after `attempts`, same as a
    bare os.replace() would, just not on the first collision.

    POSIX rename() has no such failure mode at all (a reader who already has
    the old inode open keeps reading it undisturbed after the rename), so this
    loop is a no-op there in practice: the first attempt always succeeds, and
    no test on that platform can exercise the retry path itself - only that a
    normal replace still works, which the persistence tests already cover.

    WHY IT LIVES HERE

    It was db._replace_with_retry(), private to the module that happened to
    need it first, while six other atomic publishes across update_list.py,
    settings_file.py and defaults.py called os.replace() bare and had the same
    hazard with none of the handling. The worst of those is the master list
    publish: a PermissionError there takes the bot's list off the air, which is
    the exact failure the atomic-publish rewrite exists to prevent.

    A Windows-versus-POSIX difference isolated from the rest of the codebase is
    what this module is for, so it is here and public rather than reached for
    through another module's underscore.
    """
    last_err = None
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as err:
            last_err = err
            if attempt < attempts - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last_err
