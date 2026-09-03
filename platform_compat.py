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
