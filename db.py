# db.py - Central state storage for DCCore
import io
import json
import os
import datetime
import tempfile
import threading
import platform_compat
import runtime
import defaults as config

# Every on-disk file this module owns is small and rewritten in full, so a single
# lock serialising the writes is enough. It is deliberately NOT dcc.queue_lock:
# dcc.py calls save_dcc_queue() while already holding queue_lock, and threading.Lock
# is not reentrant, so reusing it would deadlock on the first save.
#
# Bound to runtime.py's object, not constructed here - db.py is reloaded by
# !rehash (commands.CORE_MODULES), and a fresh threading.Lock() on every reload
# would let two callers both believe they hold exclusive access to the same
# on-disk file at once. See dcc.queue_lock's own comment for the full mechanism.
_disk_lock = runtime.disk_lock

# save and load previously used two different literals ("data/..." vs "./data/...").
# One constant now, built with os.path.join so it is correct on Windows too.
DCC_QUEUE_FILE = getattr(config, "DCC_QUEUE_FILE", os.path.join("data", "dcc_queue.txt"))
SPEED_RECORD_FILE = getattr(config, "SPEED_RECORD_FILE", os.path.join("data", "speed_record.txt"))
KNOWN_BOTS_FILE = getattr(config, "KNOWN_BOTS_FILE", os.path.join("data", "known_bots.json"))
FETCHED_BOT_LISTS_FILE = getattr(config, "FETCHED_BOT_LISTS_FILE",
                                 os.path.join("data", "fetched_bot_lists.json"))
FETCH_HISTORY_FILE = getattr(config, "FETCH_HISTORY_FILE",
                              os.path.join("data", "fetch_history.json"))
NOTICES_FILE = getattr(config, "NOTICES_FILE",
                       os.path.join("data", "notices.json"))
PRIVATE_MESSAGES_FILE = getattr(config, "PRIVATE_MESSAGES_FILE",
                                os.path.join("data", "private_messages.json"))
LIST_GRABS_FILE = getattr(config, "LIST_GRABS_FILE",
                          os.path.join("data", "list_grabs.json"))


# The temp file's name, and how it is swapped in (#692, audit L28). One
# prefix and suffix, so the sweep below knows exactly what it may remove.
_SWAP_PREFIX, _SWAP_SUFFIX = ".tmp_", ".swap"


def discard_stale_swaps(directory=None):
    """Remove `.tmp_*.swap` files a previous run was killed in the middle of
    (#692, audit L28), the way update_list._discard_stale_temps() removes its
    own staging files. Returns how many went.

    _atomic_write() cleans up after an exception, but a hard kill - or a
    Ctrl-C, before this file caught BaseException there - between mkstemp
    and replace left the temp behind, and nothing ever removed it: every
    crash added a hidden file to data/. Called at startup, where this run
    has staged nothing yet, so every such file belongs to a run that is no
    longer alive.
    """
    directory = directory or os.path.dirname(os.path.abspath(DCC_QUEUE_FILE)) or "."
    try:
        entries = os.listdir(directory)
    except OSError:
        return 0
    removed = 0
    for name in entries:
        if not (name.startswith(_SWAP_PREFIX) and name.endswith(_SWAP_SUFFIX)):
            continue
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"[DB] Removed {removed} leftover temp file(s) from an earlier run in {directory}.")
    return removed


def _atomic_write(path, text, mode=None):
    """Write `text` to `path` atomically.

    `mode`: the permission bits for a file that does not exist yet. mkstemp()
    creates its file 0600 and the replace carries that through, so on POSIX
    every state file this module writes - hard_bans.txt and dcc_queue.txt,
    documented as hand-editable - became owner-only after its first save
    (#692). A file that exists keeps the mode it has (the operator's, if
    they set one); a new one gets `mode`, 0o644 unless the caller says
    otherwise - the token store says 0o600, since it holds secrets.

    Writes to a temporary file in the SAME directory (so the final step is a rename
    within one filesystem), flushes and fsyncs it, then swaps it into place.

    os.replace() is used rather than os.rename(): on Windows os.rename() raises
    FileExistsError when the destination already exists, while os.replace()
    overwrites atomically on both Windows and POSIX. See
    platform_compat.replace_with_retry()'s
    own docstring for why the replace itself is retried rather than called bare.

    A reader therefore always sees either the complete previous file or the complete
    new one - never a half-written file, and never an empty one.
    """
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=_SWAP_PREFIX, suffix=_SWAP_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            wanted = os.stat(path).st_mode & 0o777 if os.path.exists(path) else (0o644 if mode is None else mode)
            os.chmod(tmp_path, wanted)
        except OSError:
            pass  # a filesystem without modes; the content is what matters
        platform_compat.replace_with_retry(tmp_path, path)
    except BaseException:
        # BaseException, not Exception (#692): a Ctrl-C between mkstemp and
        # the replace is a KeyboardInterrupt, and the temp was left behind.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

# =================================================================----
# SECTION 1: BANS.TXT (banned users)
# =================================================================----

def load_bans_from_file():
    """Load the active bans from bans.txt into memory."""
    if not os.path.exists(config.BANS_FILE):
        return
    try:
        # #226: no encoding here used the locale ANSI code page on Windows,
        # while save_bans_to_file() (via _atomic_write) always writes utf-8.
        # A banned nick containing a byte sequence invalid in that code page
        # made the whole read raise; the bare except below caught it and
        # every active timed ban was lost - on the platform this project is
        # explicitly trying to support better.
        with open(config.BANS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if " " not in line:
                    continue
                user_key, expire_ts = line.split(" ", 1)
                try:
                    config.banned_users[user_key.lower()] = float(expire_ts)
                except ValueError:
                    print(f"[DB ERROR] Skipping malformed line in {config.BANS_FILE}: {line!r}")
        print(f"[DB] Loaded bans from {config.BANS_FILE}")
    except Exception as e:
        print(f"[DB ERROR] Could not read {config.BANS_FILE}: {e}")

def save_bans_to_file():
    """Write the active bans from memory to bans.txt, atomically."""
    try:
        with _disk_lock:
            snapshot = dict(config.banned_users)
            body = "".join(f"{user_key} {expire_ts}\n" for user_key, expire_ts in snapshot.items())
            _atomic_write(config.BANS_FILE, body)
    except Exception as e:
        print(f"[DB ERROR] Could not save to {config.BANS_FILE}: {e}")


# ---------------------------------------------------------------------------
# hard_bans.txt - permanent wildcard patterns, edited live by !ban and !unban.
#
# security.check_user_status reads this file itself on every command. That hot
# path is deliberately untouched; what follows exists because the two command
# handlers have to READ-MODIFY-WRITE it, and doing that by hand went wrong in
# three separate ways:
#
#   * !unban truncated the file with open(..., "w") and wrote the kept lines
#     back one at a time. A crash, a full disk or a kill in between leaves it
#     short or empty and the permanent bans are gone. os.replace() already
#     fixed exactly this for bans.txt, dcc_queue.txt and the rest -
#     hard_bans.txt was the last file still written in place.
#
#   * That failure mode is fail-OPEN. check_user_status only distrusts a "no
#     match" when the read RAISED; a file that is readable but truncated looks
#     identical to a file with no bans in it, so every hard-banned user is let
#     through for as long as the window lasts.
#
#   * !ban appended with f.write(f"{pattern}\n") without checking the previous
#     line ended in a newline. On a hand-edited file with no trailing newline
#     that glues two patterns into one, silently unbanning both.
#
# Both operations run in their own daemon thread, so the whole read-modify-write
# is done under a single _disk_lock acquisition: two of them interleaving would
# otherwise drop whichever entry lost the race.
# ---------------------------------------------------------------------------

def _hard_bans_path():
    return getattr(config, "HARD_BANS_FILE", os.path.join("data", "hard_bans.txt"))


def _read_hard_bans_unlocked(path):
    """Patterns from `path`, lowercased, in file order, deduplicated.

    Blank lines and #-comments are dropped. Comments are NOT preserved across a
    rewrite; security.py ignores them, and keeping them would mean tracking
    their position relative to entries that come and go.
    """
    if not os.path.exists(path):
        return []
    patterns = []
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            pattern = line.strip().lower()
            if pattern and not pattern.startswith("#") and pattern not in patterns:
                patterns.append(pattern)
    return patterns


def load_hard_bans():
    """Every permanent wildcard pattern currently on disk."""
    try:
        with _disk_lock:
            return _read_hard_bans_unlocked(_hard_bans_path())
    except Exception as e:
        print(f"[DB ERROR] Could not read {_hard_bans_path()}: {e}")
        raise


def add_hard_ban(pattern):
    """Add one pattern. Returns True if it was added, False if already present."""
    pattern = str(pattern).strip().lower()
    if not pattern:
        return False
    path = _hard_bans_path()
    with _disk_lock:
        patterns = _read_hard_bans_unlocked(path)
        if pattern in patterns:
            return False
        patterns.append(pattern)
        _atomic_write(path, "".join(f"{p}\n" for p in patterns))
    return True


def remove_hard_ban(pattern):
    """Remove one pattern. Returns True if it was removed, False if not found."""
    pattern = str(pattern).strip().lower()
    if not pattern:
        return False
    path = _hard_bans_path()
    with _disk_lock:
        patterns = _read_hard_bans_unlocked(path)
        if pattern not in patterns:
            return False
        _atomic_write(path, "".join(f"{p}\n" for p in patterns if p != pattern))
    return True


# =================================================================----
# SEKTION 2: STATS.TXT (Avancerad OmenServe-statistik)
# =================================================================----

# ---------------------------------------------------------------------------
# stats.txt - lifetime and per-day transfer counters.
#
# Same read-modify-write hazard as hard_bans.txt above, and it matters more
# here: these counters are only ever derived from their own previous value, so
# nothing recomputes them and a lost update is permanent.
#
# Up to MAX_DCC_SLOTS transfers finish concurrently, each in its own thread,
# and check_and_rotate_day() runs from the IRC read loop on every channel
# message. Holding _disk_lock across only the WRITE - which is all
# save_advanced_stats used to do - leaves the load-modify-save pair
# unsynchronised: two completions that overlap both read the same row, and
# whichever writes second discards the other's increment. A 300MB and a 7MB
# transfer finishing together added one file and 300MB instead of two files
# and 307MB.
#
# The midnight case is worse than a miscount: a transfer thread that loaded
# before check_and_rotate_day() rotated, and saves after it, writes the OLD
# date back along with un-rotated counters, so the next rotation runs a second
# time and yesterday's totals collapse to that one transfer.
#
# So every public entry point below takes _disk_lock exactly once and does the
# whole sequence under it. The _unlocked helpers exist because threading.Lock
# is not reentrant - update_stats_on_complete() rotates and saves while it is
# already holding the lock.
# ---------------------------------------------------------------------------

def _default_stats_row():
    """All zeros, dated today: the row a bot with no history starts from."""
    return [0, 0, 0, 0, 0, 0, datetime.datetime.now().strftime("%Y-%m-%d")]


def _load_advanced_stats_unlocked():
    """Parse stats.txt. Caller must hold _disk_lock.

    Raises whatever open()/read() raised when the file exists but cannot be
    read at this instant (a share-deny lock from an AV or backup tool on
    Windows, an EIO, a network share hiccup). It used to catch that and hand
    back the all-zero row, which was harmless for a display and fatal for a
    writer: update_stats_on_complete() incremented the zeros and atomically
    wrote them over the real file, and unlike the malformed-column path below
    it kept no .corrupt copy. One unreadable moment at the end of one transfer
    cost the lifetime totals, which nothing recomputes (#626). The file was
    fine - it just could not be read right then - so preserving it is wrong
    too; the only correct move for a writer is to leave it alone and say so.
    The read-only entry points (load_advanced_stats, load_advanced_stats_rolled)
    catch this themselves and keep showing zeros for that one refresh.
    """
    STATS_FILE = config.STATS_FILE
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    default_stats = _default_stats_row()

    if not os.path.exists(STATS_FILE):
        return default_stats
    # The read is its own block so the handle is CLOSED before anything tries
    # to rename the file. Renaming a file this process still has open raises
    # PermissionError on Windows - which is finding #25 in the same audit,
    # committed here while fixing #26. Caught by running it, not by reading it.
    reason = None
    with open(STATS_FILE, "r") as f:
        parts = f.read().strip().split()

    try:
        if len(parts) == 7:
            return [int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]),
                    int(parts[4]), int(parts[5]), parts[6]]
        if len(parts) == 2:
            # The supported legacy row: an old build wrote only the two
            # lifetime totals.
            return [int(parts[0]), int(parts[1]), 0, 0, 0, 0, today_str]
        # Any other column count fell straight through to the defaults with
        # NOTHING said, and the next completed transfer persisted those zeros -
        # the lifetime totals gone, and no line anywhere to say when or why.
        # Everything reaching this file now goes through _atomic_write, so it
        # takes a legacy build, an fsck or a hand edit to get here; that is an
        # argument for it being rare, not for it being silent.
        reason = f"expected 7 columns (or the legacy 2), found {len(parts)}"
    except Exception as e:
        reason = str(e)

    _preserve_corrupt_stats(STATS_FILE, reason)
    return default_stats


def _preserve_corrupt_stats(path, reason):
    """Keep an unreadable stats.txt as <name>.corrupt before it is overwritten.

    load_dcc_queue() has done this since it was written; this loader never
    did, so the one artefact that could have said what the totals used to be
    was destroyed by the next transfer. Same posture, same suffix.

    Best-effort by design: failing to preserve it must not stop the daemon
    reading its defaults and carrying on.
    """
    print(f"[DB ERROR] stats.txt is not readable ({reason}); starting from zero.")
    try:
        backup = path + ".corrupt"
        platform_compat.replace_with_retry(path, backup)
        print(f"[DB ERROR] The damaged file was kept as {backup} for manual recovery.")
    except Exception as backup_err:
        print(f"[DB ERROR] Could not even back up the damaged stats file: {backup_err}")


def _save_advanced_stats_unlocked(stats):
    """Write the 7-column row atomically. Caller must hold _disk_lock."""
    row = f"{stats[0]} {stats[1]} {stats[2]} {stats[3]} {stats[4]} {stats[5]} {stats[6]}"
    _atomic_write(config.STATS_FILE, row)


def _rotate_day_unlocked(stats):
    """Move Today to Yesterday if the date changed. Returns True if it did.

    Mutates `stats` in place and does NOT write - the caller decides when to
    save, so a rotation and an increment become one write instead of two.

    Deliberately does NOT log: the caller prints after releasing _disk_lock.
    Logging from in here would put console I/O inside the critical section, and
    worse, a print() that raises would abandon a rotation the caller had already
    decided to make. That is not theoretical - the Swedish log strings raise
    UnicodeEncodeError on any console whose code page cannot encode them, which
    is exactly how this was found.
    """
    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if stats[6] == today_str:
        return False

    # Only a one-day gap makes the Today columns yesterday's. Longer than that
    # and they are from whenever the bot last sent something - a bot idle for a
    # week would otherwise report last Tuesday's traffic as "Yesterday", which
    # is the same misdating one step further out.
    #
    # A date that will not parse, or one from the future after a clock or
    # timezone change, lands here too. Zeroing is the safe direction: it
    # under-claims where the shift over-claimed.
    if stats[6] == (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d"):
        stats[2] = stats[4]   # Yesterday files = Today files
        stats[3] = stats[5]   # Yesterday bytes = Today bytes
    else:
        stats[2] = 0
        stats[3] = 0
    stats[4] = 0
    stats[5] = 0
    stats[6] = today_str
    return True


def _coerce_file_size(file_size):
    """Best-effort integer byte count from whatever the caller passed.

    Kept verbatim from the original update_stats_on_complete: callers have been
    seen passing a list, a dict, and a decimal string. Runs OUTSIDE the lock -
    parsing does not need it.
    """
    try:
        if isinstance(file_size, list):
            file_size = file_size[0] if len(file_size) > 0 else 0
        if isinstance(file_size, dict):
            file_size = file_size.get('bytes', file_size.get('size', 0))
        return int(float(str(file_size).strip()))
    except Exception as type_err:
        print(f"[DB WARNING] Could not parse file size '{file_size}', falling back to 0: {type_err}")
        return 0


def _load_for_display_unlocked():
    """The row for a READER, which must not raise: a file that cannot be read
    this instant shows as zeros for one refresh, and nothing on this path
    writes those zeros back. Returns (row, error-or-None) so the caller can
    log after releasing _disk_lock. Caller must hold _disk_lock."""
    try:
        return _load_advanced_stats_unlocked(), None
    except Exception as err:
        return _default_stats_row(), err


def load_advanced_stats():
    """Read stats.txt. Format: total_files total_bytes yest_files yest_bytes today_files today_bytes last_date"""
    with _disk_lock:
        stats, unreadable = _load_for_display_unlocked()
    if unreadable is not None:
        print(f"[DB ERROR] Could not read stats.txt, using defaults: {unreadable}")
    return stats


def set_lifetime_totals(total_files=None, total_bytes=None):
    """Replace the lifetime columns of stats.txt, leaving the day columns
    and the date as they are - under ONE _disk_lock acquisition (#690,
    audit L26).

    The stats import did this as load_advanced_stats() then
    save_advanced_stats(): two acquisitions, and a transfer completing in
    the gap - update_stats_on_complete() from the send's own thread - had
    its +1 file and +bytes on Today and Total discarded by the import's
    stale write (the lost update this file's own header describes for the
    old dcc.py code), and a day rotation in that gap was undone. Read,
    modified and written here without letting go, so what the bot did in
    between is in the row that lands. Returns the row as written.
    """
    try:
        with _disk_lock:
            row = list(_load_advanced_stats_unlocked() or [])
            while len(row) < 7:
                row.append(0)
            if total_files is not None:
                row[0] = total_files
            if total_bytes is not None:
                row[1] = total_bytes
            _save_advanced_stats_unlocked(row)
    except Exception as err:
        # As save_advanced_stats(): a stats write must not take the caller
        # down. None tells the import nothing landed.
        print(f"[DB ERROR] Could not save to stats.txt: {err}")
        return None
    return row


def save_advanced_stats(stats):
    """Write the 7-column row to stats.txt, atomically.

    An earlier version truncated the live file and then wrote into it, so a crash
    or a concurrent writer could leave a short row behind - which load_advanced_stats
    silently discards, resetting every counter to zero.
    """
    try:
        with _disk_lock:
            _save_advanced_stats_unlocked(stats)
    except Exception as e:
        print(f"[DB ERROR] Could not save to stats.txt: {e}")


def load_advanced_stats_rolled():
    """The stats row as it stands TODAY, without writing anything.

    check_and_rotate_day() is the writer: it rotates and saves, and the daemon
    calls it when a transfer completes. This is the reader's version, for a
    status display that must not write to disk to answer a GET.

    It matters because the daemon only rotates when something finishes. A bot
    that has sent nothing since midnight still has yesterday's figures sitting
    in the Today columns, and a dashboard reading the row raw would label them
    Today - wrong, and wrong in the direction that flatters the bot.

    Rolling through _rotate_day_unlocked() keeps one definition of what "a new
    day" means. Duplicating that comparison here is the second-list problem
    this codebase keeps getting bitten by.

    Mutating the row in place is safe because _load_advanced_stats_unlocked()
    builds a fresh list on every call - there is no shared row to corrupt, and
    a defensive copy here would be guarding nothing.
    """
    with _disk_lock:
        stats, unreadable = _load_for_display_unlocked()
    if unreadable is not None:
        print(f"[DB ERROR] Could not read stats.txt, using defaults: {unreadable}")
    _rotate_day_unlocked(stats)
    return stats


def check_and_rotate_day():
    """Roll Today into Yesterday at midnight. Returns the current row.

    No try/except here on purpose. An earlier draft caught everything and
    returned a freshly loaded row on failure, which meant a logging error could
    make this hand back UN-ROTATED counters that the caller would treat as
    current - silently wrong data instead of a loud failure. The original had no
    handler either; this keeps that contract. A stats.txt that cannot be read
    raises out of here too, before anything is written (#626) - the caller in
    irc.py reports it and tries again a minute later.
    """
    with _disk_lock:
        stats = _load_advanced_stats_unlocked()
        rotated = _rotate_day_unlocked(stats)
        if rotated:
            _save_advanced_stats_unlocked(stats)
    if rotated:
        print(f"[DB ROTATE] New day detected ({stats[6]}). Moving statistics to yesterday.")
    return stats


def update_stats_on_complete(file_size):
    """Count one completed transfer into the Total and Today columns.

    The whole rotate-load-increment-save sequence happens under ONE _disk_lock
    acquisition, which is the entire point of this function: dcc.py used to do
    it by hand with load and save as separate locked calls, and concurrent
    completions silently overwrote each other's increments.

    Raises when stats.txt exists but cannot be read, and writes NOTHING then:
    counting this one transfer on top of zeros would replace the lifetime
    totals with it (#626). The caller in dcc.py logs it; the transfer goes
    uncounted, which is the smaller loss by far.
    """
    clean_size = _coerce_file_size(file_size)
    with _disk_lock:
        stats = _load_advanced_stats_unlocked()
        rotated = _rotate_day_unlocked(stats)
        stats[0] += 1           # Total files
        stats[1] += clean_size  # Total bytes
        stats[4] += 1           # Today files
        stats[5] += clean_size  # Today bytes
        _save_advanced_stats_unlocked(stats)
    if rotated:
        print(f"[DB ROTATE] New day detected ({stats[6]}). Moving statistics to yesterday.")
    return stats


def get_speed_record():
    """Read the saved speed record, in bytes/s, from disk.

    FIXED (issue #34): previously read a hardcoded "./data/speed_record.txt" literal
    while save_speed_record() already wrote to the SPEED_RECORD_FILE constant -
    the same split-literal class of bug the DCC_QUEUE_FILE fix removed. Both resolved
    to the same file only by coincidence, when the daemon's cwd is the repo root.
    """
    if not os.path.exists(SPEED_RECORD_FILE):
        return 0
    try:
        with open(SPEED_RECORD_FILE, "r") as f:
            return int(f.read().strip())
    except:
        return 0

def save_speed_record(new_record):
    """Save a new speed record to disk, atomically."""
    try:
        with _disk_lock:
            _atomic_write(SPEED_RECORD_FILE, str(int(new_record)))
    except Exception as e:
        print(f"[DB ERROR] Could not save the speed record: {e}")


def _read_speed_record_unlocked():
    """The saved record, or 0. Caller must hold _disk_lock."""
    if not os.path.exists(SPEED_RECORD_FILE):
        return 0
    try:
        with open(SPEED_RECORD_FILE, "r") as handle:
            return int(handle.read().strip())
    except Exception:
        return 0


def raise_speed_record_to(candidate):
    """Store `candidate` only if it beats the record. Returns the record after.

    ONE lock acquisition around read-compare-write, for the same reason
    record_download() and update_stats_on_complete() hold one across their
    whole load-increment-save: MAX_DCC_SLOTS transfers finish concurrently.

    Read and write used to be two separate acquisitions, with the comparison
    in the caller between them. Two transfers finishing together both read the
    old record, both decided they had beaten it, and whichever saved second
    won - so a 5 MB/s record was permanently replaced by a 1.2 MB/s one, and
    nothing ever recomputes it. Losing a record is not corruption, but it is
    the one number in the advert an operator cannot get back.
    """
    try:
        speed = int(candidate)
    except (TypeError, ValueError):
        return get_speed_record()
    with _disk_lock:
        current = _read_speed_record_unlocked()
        if speed <= current:
            return current
        try:
            _atomic_write(SPEED_RECORD_FILE, str(speed))
        except Exception as err:
            print(f"[DB ERROR] Could not save the speed record: {err}")
            return current
        return speed


DOWNLOAD_COUNTS_FILE = getattr(config, "DOWNLOAD_COUNTS_FILE",
                               os.path.join("data", "download_counts.json"))


def _load_download_counts_unlocked():
    """Parse download_counts.json. Caller must hold _disk_lock."""
    if not os.path.exists(DOWNLOAD_COUNTS_FILE):
        return {}
    try:
        with io.open(DOWNLOAD_COUNTS_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except Exception as err:
        print(f"[DB ERROR] Could not read the download counts, starting empty: {err}")
        return {}


def load_download_counts():
    """Every {key: {name, kind, count}} row from disk, or {} if there is none.

    Same posture as load_known_bots(): a file that will not parse costs an
    empty "most downloaded" table, not a refusal to start. These counters
    describe history and nothing else reads them, so losing them is a cosmetic
    failure - which is exactly why it must not be a loud one.
    """
    with _disk_lock:
        return _load_download_counts_unlocked()


def migrate_download_counts_to_labels():
    """Carry existing counters onto the labelled key, once - and never at the
    cost of the daemon starting.

    The wrapper is not decoration. This runs from oserve.startup(), and
    load_download_counts() two functions up states the posture every other
    reader of this file keeps: "a file that will not parse costs an empty
    'most downloaded' table, not a refusal to start. These counters describe
    history and nothing else reads them, so losing them is a cosmetic failure
    - which is exactly why it must not be a loud one."

    A migration that raises is a louder failure than the thing it was
    migrating. Found by audit: one non-dict value under a key a legacy row
    migrated onto raised AttributeError and the bot would not boot.
    """
    try:
        marker = _counter_migration_marker()
        if os.path.exists(marker):
            return 0
        moved = _migrate_download_counts_to_labels()
        _write_counter_migration_marker(marker)
        return moved
    except Exception as err:                              # noqa: BLE001
        print(f"[DB ERROR] Could not migrate the download counters: {err}. "
              f"They are left exactly as they were, and the daemon carries on.")
        return 0


def _counter_migration_marker():
    """Where "this migration has already run" is recorded.

    Beside the counters it describes, so a restored backup of the data
    directory carries its own answer with it.
    """
    return os.path.join(os.path.dirname(os.path.abspath(DOWNLOAD_COUNTS_FILE)),
                        ".download_counts_labelled")


def _write_counter_migration_marker(marker):
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with io.open(marker, "w", encoding="utf-8") as handle:
            handle.write("Written by db.migrate_download_counts_to_labels().\n"
                         "Its presence means the download counters already "
                         "carry their folder label.\nDelete it only if you "
                         "want that one-time migration attempted again.\n")
    except OSError as err:
        # Not fatal: the migration itself is idempotent enough to survive a
        # second run, and refusing to start over a marker would be the very
        # thing this whole function exists not to do.
        print(f"[DB] Could not record that the counter migration ran ({err}); "
              f"it will be attempted again on the next start.")


def _migrate_download_counts_to_labels():
    """The migration itself.

    Returns how many rows moved, for the caller's log and for the tests.

    Every install that has counters today is a single-folder one: multi-folder
    was unreachable until the dashboard could write the folder list. So the
    old bare key "Artist/Album/track.flac" is unambiguously a file in the
    FIRST configured folder, and gains that folder's label.

    Without this the change is a silent reset: every row an operator has
    accumulated stays under a key nothing will ever increment again, and the
    "most downloaded" table starts from nothing while still showing the old
    entries. #164 settled the principle for exactly this shape of change -
    one break at the moment the operator upgrades beats a quiet second one
    weeks later - and a migration means there is no break at all.

    IDEMPOTENT BY INSPECTION, with one honest gap. A key whose first component
    is already a configured label is left alone, so a second run does nothing.
    The gap: a library whose own top-level subfolder happens to share the
    library's label - D:\\Flac containing a folder also called "Flac" - has
    legacy keys that already start with "Flac", and those are skipped and stay
    unlabelled. That splits one folder's counters between two keys. Rare, and
    cosmetic when it happens, which is why it is documented rather than
    solved with a schema marker in a file whose every other key is a real row.
    """
    import library

    folders = library.folders()
    if not folders:
        return 0
    labels = {str(entry.name).lower() for entry in folders}
    primary = folders[0].name

    with _disk_lock:
        counts = _load_download_counts_unlocked()
        moves = {}
        for key, row in counts.items():
            if not isinstance(row, dict) or row.get("kind") != "file":
                continue
            # An ABSOLUTE key is not a relative path missing its label - it
            # is a file that was under no configured folder when it was
            # counted. os.path.join(label, absolute) returns the absolute path
            # unchanged, so "migrating" it rewrote the file and logged a
            # migration on every single boot while changing nothing.
            if os.path.isabs(str(key)):
                continue

            head = str(key).replace("\\", "/").split("/", 1)[0]
            if head.lower() in labels:
                continue
            moves[key] = os.path.join(primary, key)

        if not moves:
            return 0

        for old_key, new_key in moves.items():
            row = counts.pop(old_key)
            existing = counts.get(new_key)
            if isinstance(existing, dict):
                # Both spellings present: add rather than let one win, because
                # either way round would discard real downloads.
                try:
                    existing["count"] = (int(existing.get("count", 0))
                                         + int(row.get("count", 0)))
                except (TypeError, ValueError):
                    existing["count"] = row.get("count", 0)
            else:
                # Either nothing was there, or what was there is not a row
                # anything in this file wrote - a hand-edit, or a half-restored
                # backup. `row` came through the isinstance check above and is
                # real data; that is not. Keep the real one.
                #
                # This used to be `if new_key in counts:` followed by
                # counts[new_key].get(...), which is an AttributeError the
                # moment the existing value is a string. That ran at STARTUP,
                # so the whole daemon refused to boot over one bad line in a
                # file whose own loader is written to shrug off corruption.
                counts[new_key] = row

        try:
            _atomic_write(DOWNLOAD_COUNTS_FILE,
                          json.dumps(counts, indent=1, sort_keys=True,
                                     ensure_ascii=False))
        except Exception as err:
            print(f"[DB ERROR] Could not save the migrated download counts: {err}")
            return 0

    print(f"[MIGRATE] Moved {len(moves)} download counter(s) onto the "
          f"{primary!r} folder label.")
    return len(moves)


def record_download(key, name, kind):
    """Count one completed send against `key`, and save.

    The whole load-increment-save runs under ONE _disk_lock acquisition, for
    the same reason update_stats_on_complete() does: MAX_DCC_SLOTS transfers
    finish concurrently, and a load here, a mutate here and a separate save
    here let whichever thread saved second discard the other's increment -
    permanently, because nothing ever recomputes these counters.

    `key` identifies the thing; `name` is what a person should read. They are
    different on purpose. Two albums can hold a track with the same filename -
    see #110, where exactly that ambiguity sent the wrong file - so a file is
    keyed by its path relative to the library and only DISPLAYED by its
    basename. Collapsing them would inflate one row with another file's
    downloads and quietly claim a track is popular when two different tracks
    are.

    Not bounded, and it does not need to be: a bot can only send what it
    shares, so the row count is capped by the size of the library itself.
    """
    # A FALSY KEY MEANS "DO NOT COUNT THIS", and it is deliberate as well as
    # defensive. dcc.download_count_identity() answers None for the master
    # list, which is sent to everybody who types the nickname and is how a
    # person finds out what the downloads are rather than being one - see its
    # own note. Returning here keeps that decision in the one place that
    # already decides what a send counts as.
    if not key:
        return
    with _disk_lock:
        counts = _load_download_counts_unlocked()
        row = counts.get(key)
        if not isinstance(row, dict):
            row = {"name": name, "kind": kind, "count": 0}
        row["name"] = name or row.get("name") or key
        row["kind"] = kind or row.get("kind") or "file"
        try:
            row["count"] = int(row.get("count", 0)) + 1
        except (TypeError, ValueError):
            row["count"] = 1
        counts[key] = row
        try:
            _atomic_write(DOWNLOAD_COUNTS_FILE,
                          json.dumps(counts, indent=1, sort_keys=True, ensure_ascii=False))
        except Exception as err:
            print(f"[DB ERROR] Could not save the download counts: {err}")
        return row["count"]


def prune_list_artifact_download_counts():
    """Drop rows the master list left in the counters before it stopped being
    counted. Returns how many went.

    Needed because the fix alone is invisible to anybody who already has them:
    the list has been counted since these tables existed, its name carries the
    build date so there is a row per rebuild, and they sit at the top of
    "Most downloaded" where they crowd out the files the table is for.

    Two rules, because neither catches everything on its own:

      * the NAME is a list artifact - "<base>-<date>.zip|.rar",
        "<base>-FULL-<date>.txt" - which finds them wherever they were keyed.
      * the KEY is an absolute path inside LOCAL_LIST_DIR, which finds them
        even after the operator has renamed the bot, since the name rule is
        anchored on the CURRENT LIST_BASE_NAME and an old row would no longer
        match it. That directory holds the further lists' subdirectories too,
        so one check covers every list a bot serves.

    Nothing served can legitimately be keyed that way: a library folder is
    keyed by its label and the path beneath it, never absolutely - which is
    the whole point of library_count_key().

    Same posture as migrate_download_counts_to_labels() next door, and for the
    same reason: this runs from oserve.startup(), and these counters describe
    history that nothing else reads. Losing them is cosmetic; refusing to boot
    over them is not. So it never raises.
    """
    try:
        # Both imported here, not at module scope: dcc imports db, so a
        # top-level `import dcc` would close the cycle, and list_mod is only
        # wanted for this one sweep.
        import dcc
        import list as list_mod

        lists_root = os.path.abspath(
            getattr(config, "LOCAL_LIST_DIR", "./lists") or "./lists")

        with _disk_lock:
            counts = _load_download_counts_unlocked()
            doomed = []
            for key, row in counts.items():
                name = row.get("name") if isinstance(row, dict) else None
                if list_mod.is_list_artifact_name(name or key):
                    doomed.append(key)
                    continue
                if os.path.isabs(str(key)) and dcc.is_safe_path(lists_root, str(key)):
                    doomed.append(key)
            if not doomed:
                return 0
            for key in doomed:
                del counts[key]
            _atomic_write(DOWNLOAD_COUNTS_FILE,
                          json.dumps(counts, indent=1, sort_keys=True,
                                     ensure_ascii=False))
        print(f"[DB] Removed {len(doomed)} master-list row(s) from the "
              f"download counters; the list is not a download.")
        return len(doomed)
    except Exception as err:
        print(f"[DB] Could not tidy the download counters ({err}); "
              f"the master list may still appear under Most downloaded.")
        return 0


def top_downloads(limit=10, kind=None):
    """The most-sent items, highest first, as [{name, kind, count}].

    `kind` filters to "file" or "album". They are counted together and
    reported apart on purpose: a 700 MB album and a 4 MB track are not
    comparable, and one merged table would simply rank by whichever kind the
    bot happens to send more of, which says more about the library than about
    what people want.

    Ties break on name so the order is stable between calls - a table that
    reshuffles equal rows on every poll looks like it is changing when it is
    not.
    """
    rows = []
    for key, row in load_download_counts().items():
        if not isinstance(row, dict):
            continue
        try:
            count = int(row.get("count", 0))
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        row_kind = str(row.get("kind") or "file")
        if kind is not None and row_kind != kind:
            continue
        rows.append({"name": str(row.get("name") or key),
                     "kind": row_kind,
                     "count": count})
    rows.sort(key=lambda entry: (-entry["count"], entry["name"].lower()))
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 10
    return rows[:limit]


def load_known_bots():
    """The bot registry from disk, or {} if there is none yet.

    A registry that will not parse is not a reason to refuse to start - it is
    rebuilt from adverts within a few minutes of connecting, so a corrupt or
    hand-edited file costs an empty sidebar until then and nothing else. Same
    posture as load_advanced_stats(), which returns defaults rather than
    raising.
    """
    if not os.path.exists(KNOWN_BOTS_FILE):
        return {}
    try:
        with io.open(KNOWN_BOTS_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            return {}
        # Every reader treats an entry as a mapping - irc.py copies it with
        # dict(), webserver reads fields off it - so one malformed entry in a
        # hand-edited file would raise in all of them rather than costing the
        # "empty sidebar" this docstring promises. Adverts rebuild it.
        return {key: value for key, value in loaded.items()
                if isinstance(value, dict)}
    except Exception as err:
        print(f"[DB ERROR] Could not read the bot registry, starting empty: {err}")
        return {}


ADMIN_TOKENS_FILE = getattr(config, "ADMIN_TOKENS_FILE", os.path.join("data", "adminchat_tokens.json"))


def load_admin_tokens():
    """{name: {"hash": ..., "created": ...}} for every paired console client
    (#550, step 3), or {} - a file that will not parse costs the paired
    clients a fresh `pair`, nothing else."""
    if not os.path.exists(ADMIN_TOKENS_FILE):
        return {}
    try:
        with io.open(ADMIN_TOKENS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception as err:
        print(f"[DB ERROR] Could not read the console token store: {err}")
        return {}


def save_admin_tokens(tokens):
    try:
        with _disk_lock:
            # Secrets: owner-only from the first write (#692).
            _atomic_write(ADMIN_TOKENS_FILE,
                          json.dumps(tokens, indent=1, sort_keys=True, ensure_ascii=False),
                          mode=0o600)
    except Exception as err:
        print(f"[DB ERROR] Could not save the console token store: {err}")


def load_list_grabs():
    """Automatic list grabbing's per-bot record (#926), or {} if there is none.
    An unreadable file costs the tries count and nothing else."""
    if not os.path.exists(LIST_GRABS_FILE):
        return {}
    try:
        with io.open(LIST_GRABS_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except Exception as err:
        print(f"[DB ERROR] Could not read {LIST_GRABS_FILE}: {err}")
        return {}


def save_list_grabs(record):
    """Write it, atomically. False when it did not land."""
    try:
        with _disk_lock:
            _atomic_write(LIST_GRABS_FILE, json.dumps(record, indent=1, sort_keys=True))
        return True
    except Exception as err:
        print(f"[DB ERROR] Could not save {LIST_GRABS_FILE}: {err}")
        return False


def save_known_bots(registry):
    """Write the bot registry, atomically, through the same lock and the same
    temp-file-then-replace the other state files use. True when it landed,
    False when it did not (#691, audit L27): the caller stamps its flush
    time by this, so a failed write is tried again on the next advert
    rather than in KNOWN_BOTS_FLUSH_SECONDS.

    Serialised from a snapshot, not the live dict: the IRC thread inserts a
    bot in place while a dashboard request flushes, and json.dumps() over a
    dict that changes size mid-iteration is a RuntimeError.
    """
    try:
        snapshot = {key: (dict(entry) if isinstance(entry, dict) else entry)
                    for key, entry in dict(registry).items()}
        with _disk_lock:
            _atomic_write(KNOWN_BOTS_FILE,
                          json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False))
        return True
    except Exception as err:
        print(f"[DB ERROR] Could not save the bot registry: {err}")
        return False


def load_fetched_bot_lists():
    """The fetched-bot-lists registry from disk, or {} if there is none yet.

    Same posture as load_known_bots(): a file that fails to parse costs an
    empty File Lists switcher until the next fetch, not a refusal to start.
    The entries this restores are references (bot/fetched_at/list_path/
    entry_count/source_zip), not parsed list content - the actual extracted
    files under FETCHED_FILES_DIR were never touched by a restart in the
    first place, only the daemon's memory of which bots they belong to.
    """
    if not os.path.exists(FETCHED_BOT_LISTS_FILE):
        return {}
    try:
        with io.open(FETCHED_BOT_LISTS_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            return {}
        # Per ENTRY, not just the whole file - the same filter, and the same
        # reasoning, as load_known_bots() forty lines above. Every reader
        # treats a row as a mapping, so one malformed value in a hand-edited
        # or half-restored file raises in all of them instead of costing the
        # empty registry this docstring promises.
        return {key: value for key, value in loaded.items()
                if isinstance(value, dict)}
    except Exception as err:
        print(f"[DB ERROR] Could not read the fetched-lists registry, starting empty: {err}")
        return {}


def save_fetched_bot_lists(registry):
    """Write the fetched-bot-lists registry, atomically. Called once per
    completed list fetch (list_fetch.py), not on a timer - unlike the bot
    registry above, nothing updates this often enough to need throttling."""
    try:
        with _disk_lock:
            _atomic_write(FETCHED_BOT_LISTS_FILE,
                          json.dumps(registry, indent=1, sort_keys=True, ensure_ascii=False))
    except Exception as err:
        print(f"[DB ERROR] Could not save the fetched-lists registry: {err}")


def load_fetch_history():
    """Every 'complete'/'failed' cross-bot fetch row from disk, or {} if
    there is none yet.

    Same posture as load_known_bots()/load_fetched_bot_lists(): a file that
    fails to parse costs an empty Downloads table until the next fetch
    finishes, not a refusal to start. The files these rows point at (via
    stored_filename) were never touched by a restart in the first place,
    only the daemon's memory of which fetch produced each one.
    """
    if not os.path.exists(FETCH_HISTORY_FILE):
        return {}
    try:
        with io.open(FETCH_HISTORY_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            return {}
        # Per ENTRY, not just the whole file - the same filter, and the same
        # reasoning, as load_known_bots() forty lines above. Every reader
        # treats a row as a mapping, so one malformed value in a hand-edited
        # or half-restored file raises in all of them instead of costing the
        # empty history this docstring promises.
        #
        # Worse here than in the registry: oserve.py loads this straight into
        # config.fetch_queue, and check_fetch_queue() walks that dict with
        # row.get("state") every two seconds. One string value therefore did
        # not degrade the view - it raised AttributeError on every tick and
        # killed the cross-bot fetch dispatcher for the life of the process.
        return {key: value for key, value in loaded.items()
                if isinstance(value, dict)}
    except Exception as err:
        print(f"[DB ERROR] Could not read the fetch history, starting empty: {err}")
        return {}


def load_notices():
    """(list of notices, state dict) from disk, or ([], {"seen_id": 0}).

    Persisted at all because the events worth a badge are exactly the ones
    that happen while nobody is looking. A kick at three in the morning that
    is gone by nine is a badge that never did its job.

    Same posture as every other store here: a file that will not parse costs
    an empty panel until the next event, not a refusal to start. And the
    entries are filtered individually - one hand-edited row must not take the
    other hundred and ninety-nine with it.
    """
    if not os.path.exists(NOTICES_FILE):
        return [], {"seen_id": 0}
    try:
        with io.open(NOTICES_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            return [], {"seen_id": 0}
        # THE ID HAS TO BE A NUMBER, not merely present (#451). seen_id two
        # lines below is already coerced with a guarded int(); a row's id was
        # not, so a hand-edited "id": "first" survived the loader and raised
        # wherever ids are compared - unread_notices(), the mark-read marker -
        # taking the whole panel out rather than the one bad row.
        rows = []
        for row in (loaded.get("notices") or []):
            if not isinstance(row, dict) or "id" not in row:
                continue
            try:
                row["id"] = int(row["id"])
            except (TypeError, ValueError):
                continue
            rows.append(row)
        state = loaded.get("state")
        if not isinstance(state, dict):
            state = {}
        seen = state.get("seen_id", 0)
        try:
            seen = int(seen)
        except (TypeError, ValueError):
            seen = 0
        return rows, {"seen_id": seen}
    except Exception as err:
        print(f"[DB ERROR] Could not read the notices, starting empty: {err}")
        return [], {"seen_id": 0}


def load_private_messages():
    """(rows, state) of unanswered private messages, or ([], {"seen_id": 0}).

    Same posture as the notices beside it: a file that will not parse costs an
    empty panel until the next message, never a refusal to start, and rows are
    filtered individually so one hand-edited entry does not take the rest.
    """
    if not os.path.exists(PRIVATE_MESSAGES_FILE):
        return [], {"seen_id": 0}
    try:
        with io.open(PRIVATE_MESSAGES_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            return [], {"seen_id": 0}
        rows = [row for row in (loaded.get("messages") or [])
                if isinstance(row, dict) and "id" in row]
        state = loaded.get("state")
        if not isinstance(state, dict):
            state = {}
        try:
            seen = int(state.get("seen_id", 0))
        except (TypeError, ValueError):
            seen = 0
        # Rebuilt entry by entry, same as the rows above: one unparseable
        # timestamp in a hand-edited file must not cost the whole record of
        # who has already been told, which would make the bot repeat itself
        # to everybody at once.
        declined = {}
        for name, when in (state.get("declined") or {}).items():
            try:
                declined[str(name).lower()] = float(when)
            except (TypeError, ValueError):
                continue
        return rows, {"seen_id": seen, "declined": declined}
    except Exception as err:
        print(f"[DB ERROR] Could not read the private messages, starting "
              f"empty: {err}")
        return [], {"seen_id": 0}


def save_private_messages(rows, state):
    """Both together and atomically, for the reason save_notices() gives: a
    seen marker higher than any surviving row silently swallows everything in
    between."""
    try:
        with _disk_lock:
            _atomic_write(PRIVATE_MESSAGES_FILE, json.dumps(
                {"messages": list(rows), "state": dict(state)},
                indent=1, sort_keys=True, ensure_ascii=False))
    except Exception as err:
        print(f"[DB ERROR] Could not save the private messages: {err}")


def save_notices(rows, state):
    """Write the notices and the read marker together, atomically.

    ONE FILE, because they are one fact. Written apart, a crash between the
    two writes could leave a seen_id higher than any surviving notice - which
    silently swallows everything that arrived in between, and the operator is
    never told what they were never told about.
    """
    try:
        with _disk_lock:
            _atomic_write(NOTICES_FILE, json.dumps(
                {"notices": list(rows), "state": dict(state)},
                indent=1, sort_keys=True, ensure_ascii=False))
    except Exception as err:
        print(f"[DB ERROR] Could not save the notices: {err}")


def save_fetch_history(rows):
    """Write the finished-fetch history, atomically. Called from
    dcc_fetch.py's dispatcher tick (every 2s, skipped when nothing changed)
    and immediately on a dashboard delete, so a row disappears from disk
    right away rather than only up to one tick later."""
    try:
        with _disk_lock:
            _atomic_write(FETCH_HISTORY_FILE,
                          json.dumps(rows, indent=1, sort_keys=True, ensure_ascii=False))
    except Exception as err:
        print(f"[DB ERROR] Could not save the fetch history: {err}")


def save_dcc_queue():
    """Persist the DCC queue, leaving out users whose list is now empty.

    Previously this truncated dcc_queue.txt and then serialised straight into the open
    handle. Any crash, disk-full or concurrent writer between those two steps left a
    truncated file - and load_dcc_queue() treats an unparseable file as "start empty",
    so the entire queue disappeared silently on the next boot.

    This function READS config.dcc_queue and never writes to it (#606). It used
    to pop the emptied keys from the live dict as well, and two of its callers
    run after their `with queue_lock:` block has closed - so that pop raced the
    lock-held live iterations in dcc.py (next_waiting_pack_owner, the temp-
    archive cleanup in start_dcc_send's finally) and raised "dictionary
    changed size during iteration" in THEIR thread. Emptied keys are now
    dropped by the code that empties them, under queue_lock, in
    dcc.release_queue_entry(); the file never held them either way.
    """
    import json

    try:
        # ONE COPY, THEN WALK THE COPY (#452). The loop below used to walk
        # config.dcc_queue live while holding only _disk_lock - which guards
        # the FILE, not the dict. Every writer mutates it under queue_lock,
        # so a key added or removed mid-walk raised "dictionary changed size
        # during iteration" and the whole save was abandoned: the queue stayed
        # only in RAM until the next successful save, and a restart in between
        # lost it.
        #
        # queue_lock cannot be taken here - most callers in dcc.py and
        # commands.py are already inside `with queue_lock:` and it is a plain
        # threading.Lock, so locking would deadlock the request path. dict()
        # copies the mapping in one step under the GIL, which is what #432
        # settled on for get_total_queued_count() for the same reason: a
        # concurrent change can leave this snapshot one entry stale, never
        # raise.
        live = dict(config.dcc_queue)

        with _disk_lock:
            snapshot = {k: list(v) for k, v in live.items() if v}
            _atomic_write(DCC_QUEUE_FILE, json.dumps(snapshot, indent=4))

        print("[DB-QUEUE] Queue structure saved and sanitised successfully.")
    except Exception as e:
        print(f"[DB-QUEUE ERROR] Could not save {DCC_QUEUE_FILE}: {e}")


def load_dcc_queue():
    """Load the persisted DCC queue at boot.

    An unreadable file still means starting empty - there is nothing else to do - but
    the damaged file is preserved rather than silently overwritten by the next save,
    so the queue can be recovered by hand instead of vanishing without trace.
    """
    import json

    file_path = DCC_QUEUE_FILE
    if not os.path.exists(file_path):
        # In place, not `= {}`: config.dcc_queue is the object runtime.py
        # holds, and rebinding it here would detach the two (see runtime.py).
        config.dcc_queue.clear()
        return
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError(f"expected a JSON object, got {type(loaded).__name__}")
        config.dcc_queue.clear()
        # ROW BY ROW, like every loader beside it (#450). The top level was
        # checked and nothing below it: a value that is not a list, or a row
        # that is not a dict, went straight into config.dcc_queue and failed
        # later - on a dispatch thread, far from the file that caused it, with
        # nothing naming the file.
        #
        # A queue file is hand-editable by design (the docs say so), so one
        # bad entry has to cost that entry and not the whole queue.
        clean = {}
        dropped = 0
        for user_key, files in loaded.items():
            if not isinstance(files, list):
                dropped += 1
                continue
            rows = [row for row in files if isinstance(row, dict)]
            dropped += len(files) - len(rows)
            if rows:
                clean[str(user_key)] = rows
        if dropped:
            print(f"[DB-QUEUE] Dropped {dropped} unusable entr(ies) from "
                  f"{DCC_QUEUE_FILE}; the rest of the queue was kept.")
        config.dcc_queue.update(clean)
        total = sum(len(v) for v in clean.values())
        print(f"[DB] Loaded {total} saved queue slot(s) for {len(loaded)} user(s) from disk.")
    except Exception as e:
        config.dcc_queue.clear()
        print(f"[DB ERROR] Could not read the saved DCC queue, starting empty: {e}")
        try:
            backup = file_path + ".corrupt"
            platform_compat.replace_with_retry(file_path, backup)
            print(f"[DB ERROR] The damaged file was kept as {backup} for manual recovery.")
        except Exception as backup_err:
            print(f"[DB ERROR] Could not even back up the damaged file: {backup_err}")
