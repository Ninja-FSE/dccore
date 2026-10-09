"""A record of finished transfers (#1068).

One row is written when a transfer ends, so a figure nobody thought of yet can
still be worked out later. A row says WHAT moved, how big it was, how fast, how
long it waited in the queue, and the nick it went to or came from, as KeepTrack
does, so the file can answer "who got what" and rank the nicks.

The nick is kept in lower case as the IRC server showed it. It is not followed
across a nick change, and no user@host and no channel is stored. forget_nick()
and forget_all() remove it again and then rebuild the file (VACUUM), so a removed nick cannot be
read back out of it afterwards. Deleting alone does not do that: once the record outgrows one page the
nick is also in the index's interior pages and in the free space of pages an earlier write split.

Every transfer that ends is written, with how it ended in `status` (#1203):
completed, failed, cancelled, or pack_failed for a folder that could not be
packed. Before #1203 only completed ones were, and a file from then reads
every old row as completed. Every figure but the outcomes table counts the
completed rows alone, as it always has. A write that fails is printed and
dropped, and never reaches the transfer that called it.

A write happens on the transfer thread before its slot is released, so it waits
at most WRITE_TIMEOUT seconds for a busy file. A read takes no lock of its own
and so never holds a transfer up, however long a query over many rows takes.
A damaged file is moved aside, never deleted, and a fresh one is started.

The file is in WAL mode, so a read never stops a write: a dashboard query that is
still running when a transfer ends no longer costs that transfer its row. Next to
the file there are a -wal and a -shm file while it is open; forgetting empties the
-wal, so forgotten nicks are not left in it.
"""

import os
import sqlite3
import time

import defaults as config
import runtime

SENT = "sent"
RECEIVED = "received"

KIND_FILE = "file"
KIND_ALBUM = "album"
KIND_LIST = "list"

# How a transfer ended (#1203). A transfer the bot itself called off - the
# operator cancelling a pack, a restart, a list rebuilt under a send - is
# CANCELLED and not FAILED, so the success rate does not blame the network
# for the operator's own actions. So is one the user stopped.
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_PACK_FAILED = "pack_failed"
STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_PACK_FAILED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
    id         INTEGER PRIMARY KEY,
    direction  TEXT    NOT NULL,
    nick       TEXT,
    kind       TEXT    NOT NULL,
    ended_at   INTEGER NOT NULL,
    item_key   TEXT,
    name       TEXT,
    size       INTEGER NOT NULL,
    bytes      INTEGER NOT NULL,
    seconds    REAL,
    speed      INTEGER,
    waited     REAL,
    status     TEXT    NOT NULL DEFAULT 'completed'
);
CREATE INDEX IF NOT EXISTS transfers_by_direction_and_time ON transfers (direction, ended_at);
CREATE INDEX IF NOT EXISTS transfers_by_item ON transfers (direction, kind, item_key);
CREATE INDEX IF NOT EXISTS transfers_by_nick ON transfers (nick, direction);
CREATE TABLE IF NOT EXISTS imported (
    source       TEXT    NOT NULL,
    direction    TEXT    NOT NULL,
    nick         TEXT,
    files        INTEGER NOT NULL,
    bytes        INTEGER NOT NULL,
    since        TEXT,
    imported_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS imported_by_nick ON imported (nick, direction);
CREATE TABLE IF NOT EXISTS outcomes_began (
    at  INTEGER NOT NULL
);
"""

# THE STATUS COLUMN (#1203) came after the file did. A file written before it
# is given the column on its first open, and every row already in it reads as
# completed - which it was, since nothing else was written then. The moment
# that happens, or the moment a new file is made, goes in `outcomes_began`:
# before it a failure left no row, so a success rate over a period reaching
# back past it would count the old completed rows against no failures at all.
def _migrate(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(transfers)")}
    if "status" not in columns:
        try:
            conn.execute("ALTER TABLE transfers ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
        except sqlite3.OperationalError as err:
            # Another connection opening the same old file added it first.
            if "duplicate column" not in str(err).lower():
                raise
    # Looked at first, so an open of a stamped file - every open but the
    # first - writes nothing and a read never takes the write lock. The
    # insert is one statement, so two connections opening a new file at once
    # cannot both stamp it.
    if conn.execute("SELECT 1 FROM outcomes_began LIMIT 1").fetchone() is None:
        with conn:
            conn.execute("INSERT INTO outcomes_began (at) SELECT ? WHERE NOT EXISTS"
                         " (SELECT 1 FROM outcomes_began)", (int(time.time()),))


# IMPORTED FIGURES (#1062, #1064). Totals from before this record began -
# KeepTrack's, so far - cannot be rows of `transfers`: they have no time, no
# file and no speed. They sit in `imported`, one row per source, direction and
# (for #1064) nick; nick NULL is the bot's own lifetime total. Every all-time
# figure below adds them in; a figure "since" a time leaves them out, since an
# imported total has no date beyond "before". A re-import of a source replaces
# its rows, never adds to them, and forgetting a nick or everything takes
# them too.


def _path():
    return str(getattr(config, "TRANSFER_LOG_FILE", "") or "").strip()


WRITE_TIMEOUT = 2
READ_TIMEOUT = 10


def _open(path, timeout):
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        # Zero what a write frees on every connection, so a page split or a
        # delete does not leave cell bytes behind in the free space of a page.
        conn.execute("PRAGMA secure_delete = ON")
        # With the default rollback journal a reader's shared lock stops a
        # write, so a dashboard query still reading when a transfer ends cost
        # that transfer its row. In WAL mode readers and the writer do not
        # block each other. The mode is stored in the file.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _move_aside(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside = f"{path}.corrupt-{stamp}"
    n = 1
    while os.path.exists(aside):
        n += 1
        aside = f"{path}.corrupt-{stamp}-{n}"
    os.rename(path, aside)
    # A WAL left beside a fresh file would be replayed into it and carry the damage back.
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            try:
                os.replace(path + suffix, aside + suffix)
            except OSError:
                os.remove(path + suffix)
    return aside


def _connect(path, timeout, repair=False):
    """The file open with its schema in place.

    With `repair`, a damaged file is moved aside and a new one started. Only a
    bare DatabaseError means the file's content is wrong; a locked file or a
    full disk is an OperationalError and leaves the file alone. The caller
    holds transfer_log_lock, so nothing else is using the file while it moves.
    The moved-aside file still holds its nicks: remove it by hand to be rid of them.
    """
    try:
        return _open(path, timeout)
    except Exception as err:
        if not (repair and _is_damage(err) and os.path.isfile(path)):
            raise
        _start_afresh(path, err)
        return _open(path, timeout)


def _is_damage(err):
    """True for a bare DatabaseError: the file's content is wrong. A locked file or a full disk is a subclass."""
    return type(err) is sqlite3.DatabaseError


def _start_afresh(path, err):
    aside = _move_aside(path)
    print(f"[TRANSFER-LOG] {path} is damaged ({err}); it was moved to {aside} and a new record "
          f"was started. The old file is kept and still holds its nicks.")


_INSERT = ("INSERT INTO transfers (direction, nick, kind, ended_at, item_key, name,"
           " size, bytes, seconds, speed, waited) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
# The same with the status last (#1203). Without it a row is completed, the
# column's default, as every row was before.
_INSERT_WITH_STATUS = ("INSERT INTO transfers (direction, nick, kind, ended_at, item_key, name,"
                       " size, bytes, seconds, speed, waited, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")


# How far before outcomes_began a row may be timed and still be one whose time
# was taken just before the open that wrote the stamp (#1203).
_STAMP_RACE_SECONDS = 2


def _record(row):
    """Write one row: the columns in _INSERT's order, and a status after them
    for a transfer that did not complete."""
    path = _path()
    if not path:
        return False
    insert = _INSERT_WITH_STATUS if len(row) == 12 else _INSERT
    try:
        with runtime.transfer_log_lock:
            conn = _connect(path, WRITE_TIMEOUT, repair=True)
            try:
                # NOT BEFORE THE RECORD OF OUTCOMES BEGAN (#1203). The row's
                # time was taken by the caller, before this open - and the
                # first open of an older file is what stamps outcomes_began,
                # with its own, later clock. Across a second boundary the
                # first failure written after an upgrade fell before the stamp
                # and out of every success rate (seen on Windows CI, where
                # that first open is slow). Only such a row - timed a moment
                # before this very open - is lifted to the stamp, by a second
                # or two at most; a row that really is older (an import, a
                # test's backdated row) keeps its time.
                began = conn.execute("SELECT MIN(at) FROM outcomes_began").fetchone()[0]
                if began is not None and began - _STAMP_RACE_SECONDS <= row[3] < began:
                    row = row[:3] + (int(began),) + row[4:]
                try:
                    with conn:
                        conn.execute(insert, row)
                except sqlite3.DatabaseError as err:
                    # Opening reads only the header and the schema, so a file
                    # damaged further in opens fine and fails here, on every
                    # write from then on (#1087). Same remedy as at open: move
                    # it aside, start a new one, and write this row once more.
                    if not _is_damage(err):
                        raise
                    conn.close()
                    _start_afresh(path, err)
                    conn = _open(path, WRITE_TIMEOUT)
                    with conn:
                        conn.execute(insert, row)
            finally:
                conn.close()
        return True
    except Exception as err:
        print(f"[TRANSFER-LOG ERROR] Could not record the transfer: {err}")
        return False


def _whole(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _nick(value):
    value = str(value or "").strip().lower()
    return value or None


def record_sent(kind, item_key, name, size, wire_bytes, seconds, speed, waited, nick=None):
    """One completed send to `nick`.

    `speed` is None when the transfer was too small to measure (see
    stats_mgr.speed_is_measurable), `waited` None when the queue row carried no
    time of its own (a row saved before this existed). A list is stored without
    a name, since its name carries the build date and nothing is ranked by it.
    """
    if kind == KIND_LIST:
        item_key = name = None
    return _record((SENT, _nick(nick), kind, int(time.time()), item_key, name, _whole(size),
                    _whole(wire_bytes), _positive(seconds),
                    None if speed is None else _whole(speed),
                    None if waited is None else max(0.0, float(waited))))


def record_received(kind, size, nick=None):
    """One completed download from the bot `nick`: how big, and not what it was called.

    No file name, so nothing about what another bot shares is kept.
    """
    return _record((RECEIVED, _nick(nick), kind, int(time.time()), None, None, _whole(size),
                    _whole(size), None, None, None))


def record_unfinished(direction, status, kind, size, reached, nick=None, item_key=None, name=None):
    """One transfer that ended without completing (#1203): how it ended, how
    big it was and how many bytes reached the other side before it did.

    As with a completed row, a list keeps no name and a received transfer
    keeps none either. There is no speed and no wait: neither means anything
    for a transfer that did not finish. Returns False, writing nothing, for a
    status that is not one of the three an unfinished transfer can have.
    """
    if direction not in (SENT, RECEIVED) or status not in (STATUS_FAILED, STATUS_CANCELLED, STATUS_PACK_FAILED):
        return False
    if kind == KIND_LIST or direction == RECEIVED:
        item_key = name = None
    return _record((direction, _nick(nick), kind, int(time.time()), item_key, name, _whole(size),
                    _whole(reached), None, None, None, status))


def _query(sql, args=()):
    path = _path()
    if not path or not os.path.exists(path):
        return []
    try:
        conn = _connect(path, READ_TIMEOUT)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    except Exception as err:
        print(f"[TRANSFER-LOG ERROR] Could not read the record: {err}")
        return []


def _one(width, sql, args=()):
    rows = _query(sql, args)
    return rows[0] if rows else (None,) * width


def top_files(limit=10, since=None, kind=None):
    """The most-sent items as [(name, times sent)], most first, ties by name.

    Lists are not files and never appear here; an album (a packed folder)
    counts as one item, the way the Most downloaded table counts it. kind
    (KIND_FILE or KIND_ALBUM) keeps one of the two, as the Stats page's two
    tables show them apart (#1117); without it they come mixed.
    """
    rows = _query(
        "SELECT MAX(name), COUNT(*) AS n FROM transfers"
        " WHERE status = 'completed' AND direction = ? AND kind != ? AND (? IS NULL OR kind = ?)"
        " AND item_key IS NOT NULL AND ended_at >= ?"
        " GROUP BY item_key ORDER BY n DESC, MAX(name) LIMIT ?",
        (SENT, KIND_LIST, kind, kind, since or 0, max(0, int(limit))))
    return [(name, count) for name, count in rows]


def summary(since=None):
    """The figures the record exists for, over everything or since a Unix time.

    files_sent leaves the file lists out and lists_sent counts them; the speed
    figures come from the sends that were large enough to measure, the average
    being bytes over seconds rather than a mean of means; queue_wait_seconds is
    the mean time from being asked for to the send starting, over the rows that
    know it.
    """
    since = since or 0
    sent = _one(3,
        "SELECT SUM(kind != ?), SUM(kind = ?), SUM(CASE WHEN kind != ? THEN bytes ELSE 0 END)"
        " FROM transfers WHERE status = 'completed' AND direction = ? AND ended_at >= ?",
        (KIND_LIST, KIND_LIST, KIND_LIST, SENT, since))
    speed = _one(3,
        "SELECT MAX(speed), SUM(bytes), SUM(seconds) FROM transfers"
        " WHERE status = 'completed' AND direction = ? AND speed IS NOT NULL AND ended_at >= ?",
        (SENT, since))
    waited = _one(1,
        "SELECT AVG(waited) FROM transfers WHERE status = 'completed' AND direction = ? AND waited IS NOT NULL"
        " AND ended_at >= ?", (SENT, since))
    received = _one(2,
        "SELECT SUM(kind != ?), SUM(CASE WHEN kind != ? THEN size ELSE 0 END)"
        " FROM transfers WHERE status = 'completed' AND direction = ? AND ended_at >= ?",
        (KIND_LIST, KIND_LIST, RECEIVED, since))
    seconds = speed[2] or 0
    # All time includes the imported totals (#1062); a period does not.
    before = {SENT: (0, 0), RECEIVED: (0, 0)} if since else _imported_totals()
    return {
        "files_sent": int(sent[0] or 0) + before[SENT][0],
        "lists_sent": int(sent[1] or 0),
        "bytes_sent": int(sent[2] or 0) + before[SENT][1],
        "top_speed": int(speed[0] or 0),
        "average_speed": int((speed[1] or 0) / seconds) if seconds > 0 else 0,
        "queue_wait_seconds": float(waited[0]) if waited[0] is not None else None,
        "files_received": int(received[0] or 0) + before[RECEIVED][0],
        "bytes_received": int(received[1] or 0) + before[RECEIVED][1],
    }


def outcomes_began():
    """When failed and cancelled transfers began to be written (#1203), as a
    Unix time, or None when there is no record yet."""
    row = _one(1, "SELECT MIN(at) FROM outcomes_began")
    return int(row[0]) if row[0] is not None else None


def outcomes(since=None):
    """How the transfers of a period ended (#1203), counted per direction and kind.

    Returns {"since": the time counted from, "rows": {(direction, kind):
    {status: count}}}. Only rows from outcomes_began() on are counted, however
    far back `since` asks for: before it a failure was never written, and the
    completed rows of that time would make every rate look better than it was.
    "since" is that later time when it is the one used, so the page can say so.
    """
    began = outcomes_began()
    if began is None:
        return {"since": None, "rows": {}}
    start = max(int(since or 0), began)
    rows = {}
    for direction, kind, status, count in _query(
            "SELECT direction, kind, status, COUNT(*) FROM transfers WHERE ended_at >= ?"
            " GROUP BY direction, kind, status", (start,)):
        rows.setdefault((direction, kind), {})[status] = int(count or 0)
    return {"since": start if start > int(since or 0) else None, "rows": rows}


def _imported_totals():
    """{direction: (files, bytes)} of the bot's own imported totals."""
    totals = {SENT: (0, 0), RECEIVED: (0, 0)}
    for direction, files, size in _query(
            "SELECT direction, SUM(files), SUM(bytes) FROM imported WHERE nick IS NULL GROUP BY direction"):
        if direction in totals:
            totals[direction] = (int(files or 0), int(size or 0))
    return totals


def imported_totals(source=None):
    """The bot's imported lifetime totals as {direction: {"files", "bytes", "since"}},
    for one source or all of them. For the import's before-and-after."""
    sql = "SELECT direction, SUM(files), SUM(bytes), MIN(since) FROM imported WHERE nick IS NULL"
    args = ()
    if source:
        sql += " AND source = ?"
        args = (source,)
    figures = {}
    for direction, files, size, since in _query(sql + " GROUP BY direction", args):
        figures[direction] = {"files": int(files or 0), "bytes": int(size or 0), "since": since}
    return figures


def import_totals(source, direction, files, size, since=None):
    """Write a source's lifetime total for one direction (#1062), replacing
    whatever that source imported before for it. Returns True when it is in
    the file. Like _record(): a failure is printed and never raises."""
    path = _path()
    if not path or direction not in (SENT, RECEIVED):
        return False
    try:
        with runtime.transfer_log_lock:
            conn = _connect(path, WRITE_TIMEOUT, repair=True)
            try:
                # No rebuild (_rebuild(), #1082) here: these rows are the
                # bot's own totals and name no nick, so nothing a deleted
                # copy of one could leave in the file needs wiping. Every
                # connection zeroes what it frees anyway (_open()).
                with conn:
                    conn.execute("DELETE FROM imported WHERE source = ? AND direction = ? AND nick IS NULL",
                                 (source, direction))
                    conn.execute("INSERT INTO imported (source, direction, nick, files, bytes, since, imported_at)"
                                 " VALUES (?,?,NULL,?,?,?,?)",
                                 (source, direction, _whole(files), _whole(size), since or None, int(time.time())))
                _empty_the_wal(conn, path)
            finally:
                conn.close()
        return True
    except Exception as err:
        print(f"[TRANSFER-LOG ERROR] Could not import the {direction} totals: {err}")
        return False


def top_nicks(direction=SENT, limit=10, since=None):
    """The nicks with the most files, as [(nick, files, bytes)], most files first.

    Ties go to the larger total and then to the nick. A list is not a file, so
    it counts for neither figure, and a row without a nick is not ranked.
    """
    # All time adds each nick's imported figures (#1064); a period does not.
    rows = _query(
        "SELECT nick, SUM(files), SUM(bytes) FROM ("
        " SELECT nick, (kind != ?) AS files, CASE WHEN kind != ? THEN bytes ELSE 0 END AS bytes"
        " FROM transfers WHERE status = 'completed' AND direction = ? AND nick IS NOT NULL AND ended_at >= ?"
        " UNION ALL"
        " SELECT nick, files, bytes FROM imported WHERE direction = ? AND nick IS NOT NULL AND ? = 0"
        ") GROUP BY nick HAVING SUM(files) > 0 ORDER BY 2 DESC, 3 DESC, nick LIMIT ?",
        (KIND_LIST, KIND_LIST, direction, since or 0, direction, 1 if since else 0, max(0, int(limit))))
    return [(nick, int(files), int(size)) for nick, files, size in rows]


def nick_summary(nick, since=None):
    """What one nick has had from this bot and what this bot has had from it."""
    nick = _nick(nick)
    since = since or 0
    figures = {"files_sent": 0, "lists_sent": 0, "bytes_sent": 0, "files_received": 0, "bytes_received": 0}
    if nick is None:
        return figures
    sent = _one(3,
        "SELECT SUM(kind != ?), SUM(kind = ?), SUM(CASE WHEN kind != ? THEN bytes ELSE 0 END)"
        " FROM transfers WHERE status = 'completed' AND direction = ? AND nick = ? AND ended_at >= ?",
        (KIND_LIST, KIND_LIST, KIND_LIST, SENT, nick, since))
    received = _one(2,
        "SELECT SUM(kind != ?), SUM(CASE WHEN kind != ? THEN size ELSE 0 END)"
        " FROM transfers WHERE status = 'completed' AND direction = ? AND nick = ? AND ended_at >= ?",
        (KIND_LIST, KIND_LIST, RECEIVED, nick, since))
    figures.update(files_sent=int(sent[0] or 0), lists_sent=int(sent[1] or 0), bytes_sent=int(sent[2] or 0),
                   files_received=int(received[0] or 0), bytes_received=int(received[1] or 0))
    if not since:
        # Its imported figures (#1064): all time only, as everywhere.
        for direction, files, size in _query(
                "SELECT direction, SUM(files), SUM(bytes) FROM imported WHERE nick = ? GROUP BY direction",
                (nick,)):
            key = "sent" if direction == SENT else "received" if direction == RECEIVED else None
            if key:
                figures[f"files_{key}"] += int(files or 0)
                figures[f"bytes_{key}"] += int(size or 0)
    return figures


def has_imported():
    """True when anything from before the record began is in it: a source's
    totals or a nick's figures (#1102)."""
    return bool(_query("SELECT 1 FROM imported LIMIT 1"))


# The status last (#1203): a script that reads the columns by position
# before it still finds every one where it was.
EXPORT_COLUMNS = ("ended_at", "direction", "nick", "kind", "name", "size", "bytes",
                  "seconds", "speed", "waited", "status")
EXPORT_CHUNK = 1000


def iter_rows(since=None):
    """Every row of the record from a Unix time on (all of them with none),
    in the order written - which is the order they ended - as tuples in
    EXPORT_COLUMNS order, for the CSV export (#1102). The imported totals are
    not rows and are not here.

    Read EXPORT_CHUNK rows at a time on a connection of its own, closed before
    the rows are handed on. One cursor held open for the whole export kept a
    read snapshot as long as the download ran - for ever, with a client that
    stopped reading - and while it did, a forget could not empty the WAL and
    the forgotten nick stayed readable there (#1102 review)."""
    path = _path()
    if not path or not os.path.exists(path):
        return
    after = 0
    while True:
        conn = _connect(path, READ_TIMEOUT)
        try:
            rows = conn.execute(
                f"SELECT id, {', '.join(EXPORT_COLUMNS)} FROM transfers"
                " WHERE id > ? AND ended_at >= ? ORDER BY id LIMIT ?",
                (after, since or 0, EXPORT_CHUNK)).fetchall()
        finally:
            conn.close()
        for row in rows:
            yield row[1:]
        if len(rows) < EXPORT_CHUNK:
            return
        after = rows[-1][0]


def imported_nicks(source):
    """How many nicks a source imported per direction, as {direction: count} (#1064)."""
    return {direction: int(count or 0) for direction, count in _query(
        "SELECT direction, COUNT(*) FROM imported WHERE source = ? AND nick IS NOT NULL GROUP BY direction",
        (source,))}


def import_nicks(source, rows, since=None):
    """Write a source's per-nick totals (#1064): `rows` is [(direction, nick,
    files, bytes)], one per nick and direction. Replaces everything that
    source imported per nick before - a second import never adds to the
    first. The rows replaced name nicks, so the file is rebuilt after, as a
    forget does (#1099), and the WAL emptied. Returns how many rows were
    written, or None when it could not be written; never raises."""
    path = _path()
    if not path:
        return None
    clean = [(d, _nick(n), _whole(f), _whole(b)) for d, n, f, b in rows
             if d in (SENT, RECEIVED) and _nick(n)]
    try:
        with runtime.transfer_log_lock:
            conn = _connect(path, WRITE_TIMEOUT, repair=True)
            try:
                now = int(time.time())
                with conn:
                    conn.execute("DELETE FROM imported WHERE source = ? AND nick IS NOT NULL", (source,))
                    conn.executemany(
                        "INSERT INTO imported (source, direction, nick, files, bytes, since, imported_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        [(source, d, n, f, b, since or None, now) for d, n, f, b in clean])
                # A nick in the earlier import and not in this one would
                # otherwise stay readable in the file (#1082).
                _rebuild(conn, path)
                _empty_the_wal(conn, path)
            finally:
                conn.close()
        return len(clean)
    except Exception as err:
        print(f"[TRANSFER-LOG ERROR] Could not import the per-nick totals: {err}")
        return None


def _delete(*statements):
    """Run each (sql, args) delete in one transaction, then rebuild the file
    once. One forget takes rows from more than one table (#1064), and a
    rebuild per table rewrote the whole file twice."""
    path = _path()
    if not path or not os.path.exists(path):
        return 0
    with runtime.transfer_log_lock:
        conn = _connect(path, READ_TIMEOUT, repair=True)
        try:
            with conn:
                removed = sum(conn.execute(sql, args).rowcount for sql, args in statements)
            # The delete zeroes the cells it frees, but not the key copies in
            # an index's interior pages, nor bytes that writes made before
            # secure_delete was set left in the free space of live pages. A
            # rebuild leaves neither. The file is written through the WAL, so
            # the checkpoint after it is what puts the rebuilt pages in place.
            _rebuild(conn, path)
            _empty_the_wal(conn, path)
            return removed
        finally:
            conn.close()


def _rebuild(conn, path):
    """Rewrite the file so nothing deleted is left in it. A failure is told, never raised."""
    try:
        conn.execute("VACUUM")
    except sqlite3.Error as err:
        print(f"[TRANSFER-LOG] The rows are removed from the record, but {path} could not be rebuilt "
              f"({err}); a forgotten nick may still be readable in the file.")


def _empty_the_wal(conn, path):
    """Truncate the write-ahead log, where the rows just removed are still readable.

    In WAL mode a delete is first written to the -wal file and the main file
    is not touched until a checkpoint, so until then the forgotten nicks are
    still in both. TRUNCATE copies the change over and cuts the log to nothing.
    A reader that is still open can hold that up; it is waited for briefly
    (WRITE_TIMEOUT, so this never holds the lock longer than a send would
    wait), and if it does not finish the operator is told, because the rows
    are then still in the file until a later checkpoint empties the log (a
    checkpoint that is not a TRUNCATE reuses the log from its start and leaves
    the frames past the new writes as they were).
    """
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(WRITE_TIMEOUT * 1000)}")
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    except sqlite3.Error as err:
        busy = err
    if busy:
        print(f"[TRANSFER-LOG] Forgotten rows are removed from the record but may still be in "
              f"{path}-wal until a reader that is open has finished ({'busy' if busy == 1 else busy}).")


def forget_nick(nick):
    """Take one nick out of the record. The rows go - failed and cancelled ones
    as well as completed (#1203); the figures that do not name a nick go with them."""
    nick = _nick(nick)
    if nick is None:
        return 0
    # Its imported figures too (#1064): forgetting a nick forgets all of it.
    return _delete(("DELETE FROM transfers WHERE nick = ?", (nick,)),
                   ("DELETE FROM imported WHERE nick = ?", (nick,)))


def forget_all():
    """Empty the record, imported figures included. Returns how many rows were removed."""
    return _delete(("DELETE FROM transfers", ()), ("DELETE FROM imported", ()))
