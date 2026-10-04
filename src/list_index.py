# list_index.py - the cross-list search index for the dashboard's List Browser
"""One SQLite database holding every fetched bot list, searchable at once.

WHY THIS EXISTS AT ALL

The List Browser's filter bar searches every list you hold in one go. Doing
that by re-reading the files is arithmetically out of reach, not merely slow:
`list_fetch` re-parses a list fresh on every call - deliberately, since #76
removed unbounded retention - and #133 measured one 719k-file list at 2.0s and
ten held lists at about 11s. No amount of debouncing turns eleven seconds into
typing.

WHY FTS5 AND NOT A PLAIN TABLE

#133 proposed an ordinary indexed table and called it "milliseconds across
millions of rows". That is true of ONE of the two queries this feature needs
and false of the other, which is worth writing down because the false half is
the headline behaviour.

Measured here against 4,000,000 rows in ten lists, the sizes #133's own
channel capture recorded:

    plain table, LIKE '%term%'   page  1-41ms     which-bots  1150-1400ms
    FTS5, per-bot LIMIT 1        page   1- 4ms    which-bots     2-  4ms

Paging is fast either way because LIMIT stops the scan early. "Which bots have
no match", the question that greys out the sidebar, has no such escape: it must
prove a negative for every bot, which is a full scan per keystroke.

FTS5 with `bot` as an INDEXED column is what fixes it. The existence question
is then asked once per bot as `bot:"<name>" AND (terms)` with LIMIT 1, and each
one stops at its first hit instead of enumerating every match.

WHAT THIS CHANGES ABOUT MATCHING

FTS5 tokenises; `find_matching_entries()` does substring. So "andma" no longer
finds "Sandman" - a mid-word fragment is not a token. Whole words and prefixes
both work, and the filter bar appends a prefix wildcard to the last word so
typing behaves the way a search box is expected to.

That is a real difference in behaviour and not merely an implementation
detail. It buys the feature at all: substring matching over four million rows
is the 1.4s query above. `@find` over our own list is untouched and still
substring - this index is only the dashboard's cross-list filter.

FAILURE POSTURE

Every function here is best-effort. A missing or locked database costs the
filter bar and nothing else: the lists themselves are on disk, the browser
still pages them, and each list is indexed again the next time it is fetched.
An index made before the prefix index (#1130) is rebuilt once, from the held
lists, the way a damaged one is below. A DAMAGED database is not left in
place: sqlite3 refuses it on every open, and every caller - a fetch
completing, the startup backfill, the filter bar - opens it the same way, so
nothing would ever repair it. _connect() moves
it aside as `<file>.corrupt-<timestamp>`, starts a fresh one, and the held
lists are re-indexed from disk on the next filter query. Nothing in the
daemon's serving path reads this file, so it is never a reason to refuse to
start or to fail a fetch.
"""

import os
import sqlite3
import threading
import time

import defaults as config

# Bound from runtime for the reason db.py's own lock is: !rehash re-executes
# this module, and a fresh Lock() on every reload would let two callers both
# believe they hold it. sqlite3 serialises writers itself, but the connection
# cache below is ordinary Python state and needs its own guard.
import runtime

_conn_lock = getattr(runtime, "list_index_lock", None)
if _conn_lock is None:  # pragma: no cover - runtime always defines it
    _conn_lock = threading.Lock()

# The readers' own lock, for their own connection (#1129). Always taken AFTER
# _conn_lock when both are needed, never the other way round: see runtime.py.
_read_lock = getattr(runtime, "list_index_read_lock", None)
if _read_lock is None:  # pragma: no cover - runtime always defines it
    _read_lock = threading.Lock()

INDEX_FILE = getattr(config, "LIST_INDEX_FILE",
                     os.path.join("data", "list_index.db"))

# The page the route hands back. Deliberately small: the filter bar shows the
# first screenful and the operator narrows the term, which is faster for them
# than paging thousands of rows and far cheaper here.
DEFAULT_SEARCH_LIMIT = 200
MAX_SEARCH_LIMIT = 2000

# How many held lists one search statement names in its `bot IN (...)`. SQLite
# before 3.32 refuses a statement with more than 999 parameters, and an
# operator who grabs every list on a busy network can hold that many. Past it,
# search() asks one batch after another until the page is full.
_HELD_KEYS_PER_QUERY = 500

# 2: the prefix index and folder ids below (#1130, #1135). One version for
# both, so an upgrade rebuilds the index once and not twice.
_SCHEMA_VERSION = 2

# The FTS5 table's options. Kept as one string because _open() also looks for
# it in the stored CREATE statement: CREATE ... IF NOT EXISTS keeps a table
# made without them, so an index from before them would never get them.
#
# prefix='1 2 3 4' - THE PREFIX INDEX (#1130). build_match_query() puts a
# wildcard on the word being typed, and without an index for prefixes FTS5
# answers "al"* by merging the doclist of every token that starts with "al" -
# and bots_with_a_match() asks that once per held list. Measured on 2.64M rows
# in three lists, the keystroke "al" took about 2.8 s and a 19-keystroke
# sequence 8.2 s; with prefix indexes for 1 to 4 characters, 2 ms and 0.1 s,
# with the same rows answered in the same order. The 1 is for the last word
# of a phrase, which gets its wildcard from its first letter on ("love m"*).
# It costs disk - about 70% on its own, measured on 880k rows, and a third
# with the folder ids below - and each list takes about twice as long to
# index, which no longer holds the filter bar since #1129.
#
# columnsize=0 (#1135). FTS5 otherwise keeps a per-row record of each
# column's token count, and nothing reads it but bm25() and rank, which
# nothing here uses: search() has no ORDER BY. About 5% of the file.
_FTS_OPTIONS = "tokenize='unicode61', prefix='1 2 3 4', columnsize=0"

# One connection, reused. Opening a database per keystroke would be the
# cheapest thing here to get wrong.
_connection = None
_connection_path = None
# And one for the dashboard's readers, also reused (#1129). The writer above
# held _conn_lock for the whole of a list being indexed, about nine seconds at
# a realistic size, and every filter-bar keystroke waited behind it on the
# same lock and connection. A second connection to the same WAL database,
# under its own lock, answers from the last committed state instead. Only ever
# opened after _connect() has made the schema, and closed with the writer.
_read_connection = None
_read_connection_path = None
# Set by _connect() after it replaced a damaged file; consumed by the readers
# through _prepare_to_read(). A flag and not a lock, so it lives here.
_rebuild_pending = False


def _index_path():
    """The configured path, resolved fresh each call.

    Read through config every time rather than captured at import: the tests
    redirect it per case, and !rehash can move it.
    """
    return getattr(config, "LIST_INDEX_FILE", INDEX_FILE)


def _connect():
    """The writers' connection, opening and creating the schema if needed.

    The dashboard's readers have one of their own (#1129), opened only after
    this one by _open_reader(). Caller holds _conn_lock.

    Returns None when the database cannot be opened at all - a read-only
    directory, a disk that is full, sqlite3 built without FTS5. The caller
    treats that as "no index", which costs the filter bar and nothing else.

    A DAMAGED file is the one failure that is repaired here rather than
    reported. This used to print that the filter was "off until the next
    fetch" and return None - but the fetch opens the file through this same
    function and failed the same way, as did the startup backfill, so a torn
    restore or a disk error cost the filter for the rest of the install's
    life, with a log line promising the opposite on every keystroke. The
    only recovery was deleting the file by hand, and nothing said so. The
    index is a cache of lists still on disk, so the file is moved aside (kept,
    never deleted - the operator may want to look at it) and a fresh one is
    started in its place.
    """
    global _connection, _connection_path, _rebuild_pending

    path = _index_path()
    if _connection is not None and _connection_path == path:
        return _connection

    _close_locked()
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        conn = _open(path)
    except Exception as err:
        if not (_is_damage(err) and os.path.isfile(path)):
            print(f"[LIST-INDEX] Unavailable ({err}); the cross-list filter "
                  f"is off until it can be opened. Browsing and @find are "
                  f"unaffected.")
            return None
        aside = _move_aside(path)
        if aside is None:
            return None
        try:
            conn = _open(path)
        except Exception as again:
            print(f"[LIST-INDEX] Unavailable ({again}) even after moving the "
                  f"damaged index to {aside}; the cross-list filter is off "
                  f"until it can be opened. Browsing and @find are unaffected.")
            return None
        print(f"[LIST-INDEX] {path} was damaged ({err}); moved it to {aside} "
              f"and started a fresh index. The lists you hold are indexed "
              f"again from disk on the next filter query; browsing and @find "
              f"are unaffected.")
        _rebuild_pending = True

    _connection = conn
    _connection_path = path
    return conn


def _open(path):
    """Open `path` and make sure the schema is in it. Raises on any failure.

    THE HANDLE IS CLOSED BEFORE THE ERROR LEAVES. sqlite3.connect() is lazy -
    it succeeds on a corrupt file, on a text file, on anything openable - and
    the failure lands on the first execute() below, with a real open handle
    already in hand. Dropping it unclosed leaked one connection PER CALL, and
    _connect() is called on every search: an operator whose index file was
    damaged leaked one for every keystroke in the filter bar. On Windows the
    file also stays locked until the object is collected, so the next attempt
    fails for a new reason and the log stops describing the original one -
    and the move-aside in _connect() could not rename it at all.

    AN INDEX FROM BEFORE SCHEMA 2 IS REBUILT (#1130, #1135). FTS5 cannot add
    a prefix index or drop the column sizes on an existing table, and its
    folder column held text where it now holds an id, so the old table is
    dropped and made again, in one transaction with the CREATE: the file
    never holds no table at all. The rows come back the way a repaired file's
    do - the held lists are indexed again from disk, by the startup backfill
    or before the first filter query answers - so nothing a held list had is
    lost for good. The check reads the table's own stored CREATE statement
    rather than the meta row, which every earlier version wrote on every
    open: an index the old code reopened keeps its options and is not
    rebuilt twice.
    """
    global _rebuild_pending
    conn = None
    try:
        # check_same_thread=False: a fetch completing writes from the
        # transfer thread and the startup backfill from its own, and every
        # caller here holds _conn_lock for the whole operation anyway.
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'entries'").fetchone()
        rebuilt = stored is not None and _FTS_OPTIONS not in str(stored[0])
        if rebuilt:
            conn.execute("DROP TABLE entries")
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5("
            # bot is INDEXED - that is the whole point. It lets the existence
            # question below be asked per bot and stop at the first hit,
            # instead of enumerating every match to find out who is missing.
            # folder holds an id into `folders`, not the heading (#1135).
            "bot, filename, folder UNINDEXED, size UNINDEXED, "
            + _FTS_OPTIONS + ")")
        # EACH FOLDER ONCE (#1135). Every row stored its full heading, which
        # repeats about nine times per folder at the median, and the index was
        # larger than the list text it indexes. A heading is stored here once
        # per list, and the row holds its id. Keyed by the index name, as the
        # rows are, so a list's folders go with its rows.
        conn.execute("CREATE TABLE IF NOT EXISTS folders "
                     "(id INTEGER PRIMARY KEY, bot TEXT, folder TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS folders_by_bot "
                     "ON folders (bot)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)",
                     (str(_SCHEMA_VERSION),))
        conn.commit()
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        raise
    if rebuilt:
        print("[LIST-INDEX] Rebuilding the search index once, to make "
              "short prefixes in the dashboard's filter fast: the lists you "
              "hold are indexed again from disk, up to about a minute per "
              "million files, and the file comes out about half as big again "
              "as before. Browsing and @find are unaffected.")
        # The same flag a repaired file sets: the readers run the backfill
        # before they answer, so an emptied table is never reported as
        # holding no match even when no startup backfill ran first.
        _rebuild_pending = True
    return conn


def _is_damage(err):
    """True when sqlite3 says the FILE is wrong, not the environment.

    sqlite3 raises the bare DatabaseError for exactly the result codes that
    mean the file's content is unusable - SQLITE_NOTADB ("file is not a
    database") and SQLITE_CORRUPT ("database disk image is malformed").
    Everything environmental - a locked file, a full disk, a directory that
    cannot be written, a build without FTS5 - is an OperationalError, a
    SUBCLASS, and must not match: moving a healthy index aside because the
    disk was full for a moment would throw away an index that was fine.
    """
    return type(err) is sqlite3.DatabaseError


def _move_aside(path):
    """Rename a damaged index, and its WAL sidecars, out of the way.

    Returns the new path, or None with the reason printed - and then the
    operator IS told what to do, which the old message never did.

    The `-wal` and `-shm` files go with it when they are still there. sqlite3
    normally deletes them itself as the last connection closes, which the
    failed open in _open() just did; when it could not (another process
    holding them), a WAL left beside a fresh database would be replayed into
    it on open and carry the damage straight back across.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside = f"{path}.corrupt-{stamp}"
    n = 1
    while os.path.exists(aside):
        n += 1
        aside = f"{path}.corrupt-{stamp}-{n}"
    try:
        os.rename(path, aside)
    except OSError as err:
        print(f"[LIST-INDEX] {path} is damaged and could not be moved aside "
              f"({err}); the cross-list filter is off until the file is "
              f"deleted by hand. Browsing and @find are unaffected.")
        return None
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            try:
                os.replace(path + suffix, aside + suffix)
            except OSError:
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass
    return aside


def _prepare_to_read():
    """Open the index for a reader and run the rebuild a repair has flagged.

    Returns False when there is no index to read. Called by the dashboard's
    readers BEFORE they take _read_lock for their own query, because the
    rebuild cannot run from inside _connect(): backfill_missing() takes
    _conn_lock per list itself, so it runs here with neither lock held. And
    it is the readers that must not answer from the
    fresh, empty index - bots_with_a_match() would report every held list as
    empty, which is the false claim its "no index" branch exists to avoid. A
    fetch completing does not need this; writing into an empty index is fine.

    Same cost as the startup backfill, paid once, on the first filter query
    after the repair; the flag is cleared under the lock so two dashboard
    threads do not both pay it.

    THE ORDINARY KEYSTROKE TAKES NO WRITE LOCK (#1129). This took _conn_lock
    on every call, so even with a read connection of its own every keystroke
    still waited for a list being indexed to finish. _open_reader() only
    takes it when the read connection is not open yet, and a repair always
    closes that, so the flag can only be set when this goes the slow way.
    """
    global _rebuild_pending
    available = _open_reader()
    pending = False
    if _rebuild_pending:
        with _conn_lock:
            pending = _rebuild_pending
            _rebuild_pending = False
    if pending:
        backfill_missing(dict(getattr(config, "fetched_bot_lists", None) or {}))
    return available


def _open_reader():
    """Make sure the read connection is open for the configured path.

    Returns False when there is no index to read. Called with neither lock
    held. The usual answer comes from two module globals and costs nothing;
    otherwise the writer's _connect() goes first, under _conn_lock, because it
    is what creates the schema and repairs a damaged file - a reader opened
    before it would create an empty database file of its own, and one opened
    on a damaged file would hold it open where the move-aside must rename it.
    Then the read connection is opened beside it, under _read_lock taken
    inside _conn_lock: the lock order runtime.py states.
    """
    if _read_connection is not None and _read_connection_path == _index_path():
        return True
    with _conn_lock:
        if _connect() is None:
            return False
        with _read_lock:
            return _open_reader_locked()


def _open_reader_locked():
    """Open the read connection on the writer's file. Caller holds both locks.

    query_only, because nothing on this connection should ever write: a
    write here would contend with index_bot_list() for the database's own
    write lock, which is the wait this connection exists to avoid.
    """
    global _read_connection, _read_connection_path
    path = _connection_path
    if _read_connection is not None and _read_connection_path == path:
        return True
    _close_reader_locked()
    conn = None
    try:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
    except Exception as err:
        # Closed before the error leaves, for the reason _open() gives.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        print(f"[LIST-INDEX] Unavailable for reading ({err}); the cross-list "
              f"filter is off until it can be opened. Browsing and @find are "
              f"unaffected.")
        return False
    _read_connection = conn
    _read_connection_path = path
    return True


def _reader():
    """The read connection, or None if it was closed after _open_reader().
    Caller holds _read_lock. A seam the tests replace, as they do _connect()."""
    return _read_connection


def _close_reader_locked():
    global _read_connection, _read_connection_path
    if _read_connection is not None:
        try:
            _read_connection.close()
        except Exception:
            pass
    _read_connection = None
    _read_connection_path = None


def _close_locked():
    """Close both connections. Caller holds _conn_lock.

    The reader goes too, and FIRST, under its own lock - which waits for a
    query in flight rather than closing the connection under it. Every path
    that drops the writer comes through here: close(), a moved
    LIST_INDEX_FILE and the repair in _connect(), which renames the file
    straight after. On Windows a rename fails with a sharing violation while
    any handle on the file is open, so a reader left open would turn every
    repair into "could not be moved aside" (#1129).
    """
    global _connection, _connection_path
    with _read_lock:
        _close_reader_locked()
    if _connection is not None:
        try:
            _connection.close()
        except Exception:
            pass
    _connection = None
    _connection_path = None


def close():
    """Drop the cached connections. For tests, and for a rehash moving the file."""
    with _conn_lock:
        _close_locked()


def _quote(text):
    """One FTS5 string literal. Doubling the quote is the escape it defines."""
    return '"' + str(text).replace('"', '""') + '"'


def _has_tokens(name):
    """Whether unicode61 finds any token in `name` (#1091). It keeps letters
    and digits and splits on everything else, so a nick made only of IRC's
    special characters - ^_^, [_], |-| - is no phrase at all: `bot:"^_^"`
    matches no row, held or not. Those names are asked with `bot = ?` alone,
    a scan, but only for them."""
    return any(ch.isalnum() for ch in str(name))


def filter_segments(text):
    """What a filter-bar query means, as a list of phrases.

    ASKED FOR IN THE BETA, and it is the difference between a filter that
    finds an album and one that finds every track with a short word in it:

        "well when i type amon a i want it to search 'amon a' only. if i type
         amon amar i want it to search 'amon amar'. if i want the 2nd word to
         be in any place then i search for amon*amar"

    So the words somebody types are a PHRASE - adjacent, in that order - and
    "*" is what separates one phrase from another. Typing "amon a" asks for
    "amon" followed by a word starting with "a", which is Amon Amarth and not
    every track whose title happens to contain the word "a".

    That was the old behaviour: every word ANDed, in any position. It made a
    two-word query WIDER than a one-word query in every way that mattered,
    because the second word was usually short and matched half the library.

    Returns [] for nothing typed, and drops empty segments - "amon*", "*amon"
    and "amon**x" all mean the phrase either side of a separator, with
    nothing on the other.
    """
    return [part.strip() for part in str(text or "").lower().split("*")
            if part.strip()]


def build_match_query(segments, prefix_last=True):
    """The FTS5 MATCH expression for a filter-bar query, or None for nothing.

    Each segment is a PHRASE - its words must be adjacent and in order - and
    the segments are ANDed, so they may appear anywhere relative to each
    other. See filter_segments() for why round that way.

    Only the LAST segment's last word gets a prefix wildcard, and only when it
    is long enough to narrow anything. That is what makes this a filter bar
    rather than a search button: the word being typed right now is incomplete,
    and the ones before it are not.

    Terms are quoted, never interpolated: a query is whatever somebody typed
    into a box, and FTS5's expression syntax has plenty of operators in it. An
    unquoted "-" or "NEAR" would be read as syntax, and at best answers the
    wrong question. The "*" the operator types is a separator here and never
    reaches the expression - it is consumed by filter_segments().
    """
    cleaned = [str(part).strip().lower() for part in (segments or [])]
    cleaned = [part for part in cleaned if part]
    if not cleaned:
        return None

    parts = []
    for i, phrase in enumerate(cleaned):
        last = (i == len(cleaned) - 1)
        words = phrase.split()
        if not words:
            continue
        # THE PREFIX GOES ON UNLESS IT WOULD MATCH EVERYTHING. A lone "a"
        # with a wildcard is every row in the index, which is why the length
        # floor exists. Inside a PHRASE it is nothing of the sort: "amon a"*
        # is already anchored by "amon", and refusing the wildcard there is
        # what would make the query useless - "amon a" would ask for a title
        # with the standalone word "a" straight after "amon", which is not
        # what anybody types it for. They are typing "Amon Amarth".
        if prefix_last and last and (len(words) > 1 or len(words[-1]) >= 2):
            # The wildcard sits OUTSIDE the quotes: "meta"* is FTS5's prefix
            # form, and on a multi-word phrase it applies to the phrase's own
            # last token - which is exactly the word still being typed.
            parts.append(_quote(" ".join(words)) + "*")
        else:
            parts.append(_quote(" ".join(words)))
    if not parts:
        return None
    return "filename:(" + " AND ".join(parts) + ")"


def index_bot_list(bot, rows):
    """Replace everything indexed for `bot` with `rows`.

    Called from the parse list_fetch already performs at fetch time - that
    parse walked the whole file only to count it and threw the rows away, so
    the index costs one pass that was already happening.

    `rows` are the dicts list.entries_to_filelist_rows() produces, whose
    filename key is "title" - NOT "filename". This read "filename" at first,
    which is not a key that function has ever produced, so every row went into
    the index with an empty name and the whole cross-list filter matched
    nothing in production. The tests missed it by building their own rows with
    a "filename" key instead of calling the producer.

    `rows` may also be a one-pass stream of the same dicts - list.CountedRows
    over list.iter_filelist_rows(), which is how a fetch hands them over
    (#1134) - so they are counted as they go in rather than by len().

    Returns the number indexed, or 0 if the index is unavailable.
    """
    # NORMALISED, and the same normalisation on both sides of the delete.
    #
    # This stored whatever the operator typed and deleted with SQLite's binary
    # `=`, while every reader case-folds: fetched_bot_lists is keyed
    # lower-case, indexed_bots() lowers, and FTS5 MATCH folds. So fetching
    # from "Dude" and then re-fetching from "DUDE" - the ordinary case, since
    # the sidebar prefills the nick and a refetch is retyped by hand - left
    # BOTH copies in the index. The stale one answered searches beside the new
    # one, indexed_bots() reported a single bot, and nothing could ever free
    # it: at the sizes this project measures, ~80MB per orphan.
    name = str(bot or "").strip().lower()
    if not name:
        return 0
    indexed = [0]

    with _conn_lock:
        conn = _connect()
        if conn is None:
            return 0
        try:
            # A SEARCH NEVER SEES THE GAP between the delete and the insert:
            # it would read a list emptied and not yet refilled, and report
            # that bot as holding no match - the same false "empty"
            # bots_with_a_match() takes care to avoid. That used to be
            # guaranteed by readers waiting on this lock for the whole write,
            # which held every filter-bar keystroke for as long as a list
            # took to index (#1129). They now read on a connection of their
            # own, and WAL gives each of their queries a snapshot of the last
            # COMMITTED state: the old list until the commit below, the new
            # one after it, never the half-replaced one in between. This lock
            # still makes writers take turns on this connection.
            #
            # Delete first, in the same transaction as the insert: a refetch
            # that replaced a list must not leave the old rows searchable
            # beside the new ones, and a crash between the two must not leave
            # the bot indexed twice.
            conn.execute("DELETE FROM entries WHERE bot = ?", (name,))
            # And its folders, or every refetch under a new set of headings
            # would leave the old ones behind for good (#1135).
            conn.execute("DELETE FROM folders WHERE bot = ?", (name,))
            # The list's headings get ids as its rows go past, and are written
            # after them, in the same transaction: one dict of this list's
            # headings, never a second copy of its rows.
            folder_ids = {}
            next_id = conn.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM folders").fetchone()[0]
            conn.executemany(
                "INSERT INTO entries (bot, filename, folder, size) "
                "VALUES (?, ?, ?, ?)",
                # "title" is the key entries_to_filelist_rows() writes;
                # "filename" is accepted too because find_matching_entries()
                # uses that name and a future caller may reasonably pass its
                # rows straight through.
                # A GENERATOR, not a list: executemany consumes it a row at
                # a time, where a comprehension built a second complete copy
                # of the list in memory first - at the four million rows this
                # index is measured against, hundreds of megabytes of tuples
                # held for the length of the write and for no reason.
                _counted_values(name, rows, indexed, folder_ids, next_id))
            conn.executemany(
                "INSERT INTO folders (id, bot, folder) VALUES (?, ?, ?)",
                ((folder_id, name, folder)
                 for folder, folder_id in folder_ids.items()))
            conn.commit()
            _checkpoint_locked(conn)
            # Counted as they went in, not len(rows): `rows` may be a stream
            # with no length (#1134), and for a list the two are the same.
            return indexed[0]
        except Exception as err:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[LIST-INDEX] Could not index {name}'s list ({err}); the "
                  f"cross-list filter will not see it. Browsing it still works.")
            return 0


def _counted_values(name, rows, indexed, folder_ids, next_id):
    """index_bot_list()'s INSERT parameters, counting each into indexed[0].

    Each row's heading becomes an id (#1135): the one already given to that
    exact text in this list, or the next free one, recorded in `folder_ids`
    for the caller to write into `folders`. Exact text, case and all - two
    headings that differ only in case are two folders, as they were when
    each row carried its own."""
    for row in rows:
        indexed[0] += 1
        folder = str(row.get("folder") or "")
        folder_id = folder_ids.get(folder)
        if folder_id is None:
            folder_id = folder_ids[folder] = next_id + len(folder_ids)
        yield (name,
               str(row.get("title") or row.get("filename") or ""),
               folder_id,
               str(row.get("size") or ""))


def drop_bot(bot):
    """Forget one bot's list. Called when its fetched list is deleted."""
    name = str(bot or "").strip().lower()
    if not name:
        return False
    with _conn_lock:
        conn = _connect()
        if conn is None:
            return False
        try:
            conn.execute("DELETE FROM entries WHERE bot = ?", (name,))
            # Its folders too (#1135): forget_bot() and a purge come through
            # here, and a folder whose rows are gone is a leak, not a cache.
            conn.execute("DELETE FROM folders WHERE bot = ?", (name,))
            conn.commit()
            _checkpoint_locked(conn)
            return True
        except Exception as err:
            print(f"[LIST-INDEX] Could not drop {name} from the index: {err}")
            return False


def drop_every_list_of(nick):
    """Forget every list held from `nick`: the main one, indexed under the
    bare nick, and each other one, under "<nick>/<marker>".

    By the names in the index, not by the markers the held entry still
    names. Forget dropped only those, so a list a refetch had already left
    out - gone from the archive, empty now, or over the ceiling - kept its
    rows and its folders for good, and nothing else ever removed them.

    Compared with substr() and not LIKE: "_" is a LIKE wildcard and common in
    a nick, so "some_bot/%" would also drop "somexbot/rar". "somebot2" is
    never dropped with "somebot": it is neither the bare nick nor "somebot/"
    and more.
    """
    name = str(nick or "").strip().lower()
    if not name:
        return False
    prefix = name + "/"
    with _conn_lock:
        conn = _connect()
        if conn is None:
            return False
        try:
            # The same scan as drop_bot()'s `bot = ?`, which FTS5 answers by
            # reading every row: once here, instead of once per marker.
            for table in ("entries", "folders"):
                conn.execute(f"DELETE FROM {table} WHERE bot = ? "
                             f"OR substr(bot, 1, ?) = ?",
                             (name, len(prefix), prefix))
            conn.commit()
            _checkpoint_locked(conn)
            return True
        except Exception as err:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[LIST-INDEX] Could not drop {name}'s lists from the "
                  f"index: {err}")
            return False


def _checkpoint_locked(conn):
    """Force a full WAL checkpoint after a write. Caller must hold _conn_lock.

    WAL mode (set in _connect()) means every write lands in a separate
    .db-wal log first, normally folded back into the main file by SQLite's
    own automatic checkpoint. That default is PASSIVE - best-effort, and it
    can only run BETWEEN transactions, never inside one. index_bot_list()
    deletes and re-inserts a whole bot's list as one transaction, and the
    largest bot measured here is 1.3 million rows - so the log grows to that
    entire write's size before there is a transaction boundary for the
    passive checkpoint to even attempt, and if a concurrent read (the
    dashboard's filter bar, polled continuously) holds an older snapshot at
    the moment it tries, it is left incomplete. Measured on the live index:
    a 128MB .db-wal file that a manual TRUNCATE checkpoint cleared to zero
    in one call, with nothing in it in flight.

    TRUNCATE forces the checkpoint through and shrinks the log file back to
    empty rather than leaving it at whatever size the last checkpoint grew
    it to (the default PASSIVE mode's own behaviour even when it succeeds).

    The filter bar's queries run on their own connection (#1129), so one may
    be in flight at this moment. TRUNCATE then waits for it, within the
    connection's busy timeout - a query takes milliseconds - and if it is
    still there, the checkpoint reports busy and returns rather than raising.
    Either way the paragraph below holds.

    Never raises, and never rolled back into by the caller: the row change
    just committed is safe either way (in the main file or still in the
    WAL), so a checkpoint that fails costs disk space, not correctness - the
    next successful one, from here or from SQLite's own passive attempts,
    catches up.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception as err:
        print(f"[LIST-INDEX] Could not checkpoint the index after a write "
              f"({err}); the .db-wal file may stay larger than it needs to "
              f"be until the next one succeeds.")


def indexed_bots():
    """Every bot with rows in the index, lower-cased for comparison."""
    if not _open_reader():
        return set()
    with _read_lock:
        conn = _reader()
        if conn is None:
            return set()
        try:
            return {str(row[0]).strip().lower()
                    for row in conn.execute("SELECT DISTINCT bot FROM entries")}
        except Exception:
            return set()


def bots_with_a_match(terms, bots):
    """Which of `bots` have at least one row matching, and which have none.

    Returns (matched, empty), both lower-cased sets.

    Asked once per bot with LIMIT 1 rather than as one DISTINCT over every
    match. That is the difference between 2ms and 1.4s at four million rows:
    a bot WITH a match stops at its first row, and a bot without one is the
    only case that pays for a scan of its own rows alone.
    """
    query = build_match_query(terms)
    candidates = [str(b).strip() for b in (bots or []) if str(b).strip()]
    if query is None or not candidates:
        return set(), set()

    if not _prepare_to_read():
        # No index is not "no bot matches": claiming every list is empty
        # would grey out the whole sidebar and read as a broken filter.
        return set(), set()
    matched = set()
    # A bot whose own query FAILED is neither matched nor empty. Falling out
    # of `matched` used to put it straight into `empty` below, and `empty` is
    # rendered as a positive statement - the list greys out and the status
    # line counts it as holding no match. That is the same false claim the
    # "no index" branch two lines down already refuses to make, arrived at by
    # a different route: one sqlite error and a list that DOES match is shown
    # to the operator as one that does not.
    unknown = set()
    # The read connection under its own lock, never _conn_lock: a list being
    # indexed holds that for the whole write (#1129).
    with _read_lock:
        conn = _reader()
        if conn is None:
            # No index is not "no bot matches": claiming every list is empty
            # would grey out the whole sidebar and read as a broken filter.
            return set(), set()
        for bot in candidates:
            # The MATCH is a cheap PRE-FILTER, not the answer. `bot:"name"` is
            # an FTS5 PHRASE over a tokenised column, not equality: "Bot"
            # matches "Bot-2", "Bot_away" and "Bot|gone", because unicode61
            # splits on the punctuation. Holding both Bot and Bot-2 with only
            # Bot-2 matching, the sidebar left Bot undimmed and the status
            # line said "1 match in 2 lists". Every fixture in the tests was
            # token-disjoint, which is why they passed.
            #
            # So the equality goes into the QUERY, as a plain column filter
            # alongside the MATCH, rather than being applied to whatever rows
            # a LIMIT happened to return.
            #
            # It used to fetch 25 rows and compare them in Python, with a
            # comment explaining that a near-miss neighbour "can occupy the
            # first row". It can occupy all twenty-five: a bot holding fifty
            # matching files as Dude|away, beside a Dude holding one, filled
            # the window entirely and Dude was reported as having no match at
            # all - the sidebar dimmed a list that did contain the file, and
            # search() for the same term listed it. Whatever number is chosen
            # there, a busy neighbour can exceed it.
            #
            # `bot = ?` is an ordinary comparison on the stored text, so it is
            # exact where `bot:"name"` is a tokenised phrase, and LIMIT 1 is
            # then enough: one row proves the match.
            wanted = bot.strip().lower()
            # A name with no tokens is no pre-filter: the filter alone (#1091).
            match = f"bot:{_quote(wanted)} AND {query}" if _has_tokens(wanted) else query
            try:
                rows = conn.execute(
                    "SELECT bot FROM entries WHERE entries MATCH ? AND bot = ? LIMIT 1",
                    (match, wanted)).fetchall()
            except Exception as err:
                print(f"[LIST-INDEX] Could not check {bot!r} against the "
                      f"filter ({err}); its list is left unmarked rather "
                      f"than shown as empty.")
                unknown.add(wanted)
                continue
            if any(str(r[0]).strip().lower() == wanted for r in rows):
                matched.add(wanted)
    empty = {b.lower() for b in candidates} - matched - unknown
    return matched, empty


def search(terms, limit=None, bots=None):
    """A page of matches across every indexed list.

    Returns a list of {"bot", "filename", "folder", "size"}. Capped: the
    filter bar shows a screenful and the operator narrows the term, which is
    both faster for them and far cheaper here than paging a million rows.

    `bots` restricts the answer to lists we currently hold, and the caller
    always passes it. The index is not the record of what is held -
    `fetched_bot_lists` is - and the two can drift: a list file removed by
    hand, a reset store, a bot whose entry went away while its rows stayed.
    Returning a row for a list we no longer have would offer a file that
    cannot be requested, which is a worse answer than not finding it.
    """
    query = build_match_query(terms)
    if query is None:
        return []

    # The held names, _HELD_KEYS_PER_QUERY to a statement; [None] is no
    # restriction at all.
    batches = [None]
    if bots is not None:
        held = [str(b).strip() for b in bots if str(b).strip()]
        if not held:
            return []
        held_keys = sorted({b.strip().lower() for b in held})
        batches = [held_keys[i:i + _HELD_KEYS_PER_QUERY]
                   for i in range(0, len(held_keys), _HELD_KEYS_PER_QUERY)]

    if limit is None:
        limit = DEFAULT_SEARCH_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_SEARCH_LIMIT
    # Non-positive means "omitted", not "give me nothing" - and not "give me
    # one" either, which is what max(1, ...) quietly did. The same rule
    # webserver.parse_pagination_params() already states for the paging
    # routes, so the two boundaries answer a bad limit the same way.
    if limit <= 0:
        limit = DEFAULT_SEARCH_LIMIT
    limit = min(limit, MAX_SEARCH_LIMIT)

    if not _prepare_to_read():
        return []
    # The read connection, as in bots_with_a_match() (#1129).
    with _read_lock:
        conn = _reader()
        if conn is None:
            return []
        try:
            # The heading comes back from `folders` in the SAME statement
            # (#1135), so from the same snapshot: a second query after this
            # one could see a refetch that committed in between, with the
            # page's ids already deleted. A row whose folder column is not an
            # id - written by an older version into a table it did not make -
            # shows the text it holds, as it always did.
            #
            # THE HELD LISTS ARE CHOSEN IN THE QUERY, before the LIMIT. They
            # were chosen in Python, on whatever rows the LIMIT had let
            # through, and `bot:"somebot"` is a tokenised phrase that also
            # matches "somebot-2", "somebot|away" and "somebot/rar". A list
            # that is not held - offline under Online Only, or left in the
            # index by anything - filled the page, and the held list's matches
            # were not on it: none at all, behind 300 rows of a "SomeBot-2",
            # with the sidebar showing SomeBot as matched and the page not
            # marked as cut short. bots_with_a_match() already asked
            # `bot = ?` in its query, for the same reason. The phrases stay
            # as a pre-filter: they are what the full-text index answers
            # quickly.
            found = []
            for keys in batches:
                match = query
                # A nick with no letters or digits is no phrase at all (#1091):
                # `bot:"^_^"` matches no row, so with it in the OR the list's
                # rows never came back, though the sidebar said it matched.
                # Then the IN alone decides.
                if keys is not None and all(_has_tokens(k) for k in keys):
                    match = ("(" + " OR ".join(f"bot:{_quote(k)}" for k in keys)
                             + ") AND " + query)
                found += conn.execute(
                    "SELECT bot, filename, "
                    "CASE WHEN typeof(folder) = 'integer' THEN COALESCE("
                    "(SELECT f.folder FROM folders f WHERE f.id = entries.folder), "
                    "'') ELSE folder END, size FROM entries "
                    "WHERE entries MATCH ?"
                    + ("" if keys is None else
                       " AND bot IN (" + ", ".join(["?"] * len(keys)) + ")")
                    + " LIMIT ?",
                    (match, *(keys or ()), limit - len(found))).fetchall()
                if len(found) >= limit:
                    break
        except Exception as err:
            print(f"[LIST-INDEX] Search failed ({err}); returning nothing "
                  f"rather than a partial answer.")
            return []

    return [{"bot": row[0], "filename": row[1], "folder": row[2],
             "size": row[3]} for row in found]


def _holds_rows_for(conn, bot):
    """True if the index has at least one row for `bot`. Caller holds
    _conn_lock.

    Asked of the search index, not by listing every bot in the table (#1071):
    `SELECT DISTINCT bot` reads every row of an FTS5 table, which has no
    ordinary index on a column - the whole file, 3.3 GB on a live bot, about
    forty seconds of a cold start. `bot:"name"` is answered from the index
    and LIMIT 1 stops at the first row.

    The MATCH is a pre-filter, as in bots_with_a_match(): a phrase over the
    tokenised column, so "Bot" also matches "Bot-2". The equality decides,
    and is case-insensitive because indexed_bots() was: an index written
    before the names were stored lower-case still counts its bot as present.
    """
    wanted = str(bot).strip().lower()
    if not _has_tokens(wanted):
        # No letters, so no case to fold: the stored name as it is (#1091).
        row = conn.execute("SELECT 1 FROM entries WHERE bot = ? LIMIT 1",
                           (wanted,)).fetchone()
        return row is not None
    row = conn.execute(
        "SELECT 1 FROM entries WHERE entries MATCH ? AND lower(bot) = ? LIMIT 1",
        (f"bot:{_quote(wanted)}", wanted)).fetchone()
    return row is not None


def _held_lists(bot, entry):
    """[(index name, list path)] for every list a held archive has.

    The main list under the bare nick and the others as "<nick>/<marker>",
    the names list_fetch indexes them under at fetch time (#1122): backfill
    went through the main list alone, so after an index was emptied or
    repaired a bot's RAR list was never indexed again, and the filter bar
    showed a list that does match as holding nothing. An entry written before
    an archive could hold more than one list has no "lists", only its main
    list's "list_path"."""
    import list_fetch
    out = []
    lists = entry.get("lists")
    if isinstance(lists, dict):
        for marker, info in lists.items():
            if isinstance(info, dict) and info.get("list_path"):
                out.append((list_fetch.index_key(bot, marker), info["list_path"]))
    if not any(name == bot for name, _path in out) and entry.get("list_path"):
        out.insert(0, (bot, entry["list_path"]))
    return out


def backfill_missing(held, log=print):
    """Index any held list that is not in the index yet. Returns how many.

    THE UPGRADE CASE, and it is the ordinary one. `index_bot_list()` has
    exactly one caller in the daemon - a fetch completing - while held lists
    survive restarts, because `list_fetch` persists them and `oserve` restores
    them at startup. So an operator who upgrades with lists already fetched
    has a full `fetched_bot_lists` and an empty index.

    That is worse than it sounds, because the "we cannot tell" guard does not
    cover it: `_connect()` creates the database on demand, so the connection
    is NOT None, every per-bot query simply misses, and the page states
    positively that no list holds a match. The filter reads as working and
    answers wrongly, for lists that are full of matches, until each is
    re-fetched by hand.

    `held` is `config.fetched_bot_lists`. Each entry carries the `list_path`
    the fetch stored, and re-parsing it here is the same one-pass parse
    `list_fetch` does - not a second implementation of it.

    Best-effort per bot: one unreadable list costs its own row in the filter
    and nothing else, which is the posture the whole module already takes.
    """
    import list as list_mod
    import platform_compat

    done = 0
    for key, entry in (held or {}).items():
        if not isinstance(entry, dict):
            continue
        bot = str(entry.get("bot") or key).strip()
        if not bot:
            continue
        for name, path in _held_lists(bot, entry):
            # One question per held list, answered from the index (#1071) -
            # not a read of the whole table to list every bot first.
            with _conn_lock:
                conn = _connect()
                if conn is None:
                    return done
                try:
                    if _holds_rows_for(conn, name):
                        continue
                except Exception as err:
                    log(f"[LIST-INDEX] Could not check whether {name}'s list is "
                        f"indexed ({err}); leaving it as it is.")
                    continue
            if not path or not os.path.exists(platform_compat.long_path(path)):
                continue
            # Streamed into the index, not built in memory first (#1134). A
            # list that cannot be read fails inside the write, which rolls
            # back, and CountedRows keeps the error to be reported here as it
            # always was. Rows carry the bot's nick, as a fetch writes them;
            # the list is told apart by the name it is indexed under (#1122).
            rows = list_mod.CountedRows(list_mod.iter_filelist_rows(
                platform_compat.long_path(path), bot))
            indexed = index_bot_list(name, rows)
            if rows.error is not None:
                log(f"[LIST-INDEX] Could not re-read {name}'s list to index it "
                    f"({rows.error}); the filter will not see it until the next "
                    f"fetch.")
                continue
            if indexed:
                done += 1
    if done:
        log(f"[LIST-INDEX] Indexed {done} held list(s) the search index did "
            f"not have.")
    return done


def reset_for_tests():
    """Close the connection and forget the path. Tests only."""
    global _rebuild_pending
    close()
    _rebuild_pending = False
