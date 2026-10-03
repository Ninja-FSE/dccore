"""The download counters move from a JSON file into SQLite, and none is lost (#1133).

db.record_download() ran on every completed send and rewrote the whole of
download_counts.json - load, add one, dump with sort and indent, fsync,
replace - under runtime.disk_lock, which every other write in db.py needs:
56 ms a send at 10k rows and over half a second at 100k. The counters now live
in a SQLite database beside the file, a send is one upsert, and disk_lock is
not taken at all.

What the release needs from that, and what these tests hold it to:

  * a bot upgrading from v1.13.x keeps every count, through the whole startup
    sequence (import, folder labels, list-row prune), with the numbers the old
    code would have shown;
  * the import is one transaction: a process killed in the middle of it - a
    real kill, in a child process - leaves nothing behind, and the next start
    imports again with exact counts; an import that fails leaves no marker and
    loses no send;
  * the JSON is read and never written, byte for byte, so a downgrade finds the
    counts as they were at the upgrade;
  * the ranking is the same answer the JSON version gave, tie order included.
"""

import contextlib
import io
import json
import os
import random
import sqlite3
import subprocess
import sys
import textwrap
import threading
import unittest
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import db  # noqa: E402
import runtime  # noqa: E402

REPO_ROOT = support.REPO_ROOT


# ---------------------------------------------------------------------------
# The JSON version, as v1.13.x ran it: the reference every answer is held to.
# ---------------------------------------------------------------------------

def old_save(path, counts):
    """How the JSON version wrote the file: sorted by key, indented."""
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(counts, indent=1, sort_keys=True, ensure_ascii=False))


def old_load(path):
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle)


def old_ranking(counts, kind, limit):
    """top_downloads() before #1133, over the rows in file order."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 10
    rows = []
    for row_key, row in counts.items():
        if not isinstance(row, dict):
            continue
        try:
            count = int(row.get("count", 0))
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        rows.append({"name": str(row.get("name") or row_key),
                     "kind": str(row.get("kind") or "file"),
                     "count": count})
    rows.sort(key=lambda entry: (-entry["count"], entry["name"].lower()))
    ranked = {None: rows}
    for row in rows:
        ranked.setdefault(row["kind"], []).append(row)
    return [dict(row) for row in ranked.get(kind, [])[:limit]]


def old_migrate_to_labels(counts, labels, primary):
    moves = {}
    for key, row in counts.items():
        if not isinstance(row, dict) or row.get("kind") != "file":
            continue
        if os.path.isabs(str(key)):
            continue
        head = str(key).replace("\\", "/").split("/", 1)[0]
        if head.lower() in labels:
            continue
        moves[key] = os.path.join(primary, key)
    for old_key, new_key in moves.items():
        row = counts.pop(old_key)
        existing = counts.get(new_key)
        if isinstance(existing, dict):
            try:
                existing["count"] = int(existing.get("count", 0)) + int(row.get("count", 0))
            except (TypeError, ValueError):
                existing["count"] = row.get("count", 0)
        else:
            counts[new_key] = row
    return dict(sorted(counts.items()))     # the file it saved was sorted


def old_prune(counts, lists_root):
    import dcc
    import list as list_mod
    doomed = []
    for key, row in counts.items():
        name = row.get("name") if isinstance(row, dict) else None
        if list_mod.is_list_artifact_name(name or key):
            doomed.append(key)
            continue
        if os.path.isabs(str(key)) and dcc.is_safe_path(lists_root, str(key)):
            doomed.append(key)
    for key in doomed:
        del counts[key]
    return counts


class _CountsCase(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.path = os.path.join(self.tree.root, "data", "download_counts.json")
        os.makedirs(os.path.dirname(self.path))
        patch = mock.patch.object(db, "DOWNLOAD_COUNTS_FILE", self.path)
        patch.start()
        self.addCleanup(patch.stop)

    @property
    def database(self):
        return os.path.join(os.path.dirname(self.path), "download_counts.db")

    def json_bytes(self):
        with io.open(self.path, "rb") as handle:
            return handle.read()

    def raw_rows(self):
        """The table and the marker as SQLite holds them, read past db.py."""
        conn = sqlite3.connect(self.database)
        try:
            tables = {name for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            rows = (dict((key, count) for key, count in conn.execute(
                "SELECT key, count FROM download_counts"))
                if "download_counts" in tables else {})
            marked = ("download_counts_meta" in tables and conn.execute(
                "SELECT COUNT(*) FROM download_counts_meta").fetchone()[0] > 0)
            return rows, marked
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# A v1.13.x data directory, upgraded.
# ---------------------------------------------------------------------------

class ABotUpgradingFromV113KeepsItsCounts(_CountsCase):

    def setUp(self):
        super().setUp()
        self.flac = os.path.join(self.tree.root, "Flac")
        os.makedirs(self.flac)
        folders = os.path.join(self.tree.root, "library_folders.json")
        with io.open(folders, "w", encoding="utf-8") as handle:
            json.dump([{"name": "Flac", "path": self.flac}], handle)
        self.set_config(LIBRARY_FOLDERS_FILE=folders, FILE_DIRECTORY=None,
                        LIST_BASE_NAME="SomeBot", LOCAL_LIST_DIR=self.tree.lists)
        legacy = os.path.join("Artist", "Album", "01 - Intro.flac")
        self.fixture = {
            # Labelled, as every count since the labels has been.
            os.path.join("Flac", "Artist", "Album", "02 - Song.flac"):
                {"name": "02 - Song.flac", "kind": "file", "count": 12},
            # A legacy unlabelled key, AND its labelled twin: the migration adds them.
            legacy: {"name": "01 - Intro.flac", "kind": "file", "count": 4},
            os.path.join("Flac", legacy): {"name": "01 - Intro.flac", "kind": "file", "count": 3},
            # A legacy key whose labelled twin is junk: the audit's crash case.
            os.path.join("Other", "B.flac"): {"name": "B.flac", "kind": "file", "count": 6},
            os.path.join("Flac", "Other", "B.flac"): "not a row",
            # Non-ASCII names, equal in count and in lower case.
            os.path.join("Flac", "x", "ÉCOLE.flac"): {"name": "ÉCOLE.flac", "kind": "file", "count": 5},
            os.path.join("Flac", "y", "école.flac"): {"name": "école.flac", "kind": "file", "count": 5},
            os.path.join("Flac", "z", "Ωmega.flac"): {"name": "Ωmega.flac", "kind": "file", "count": 2},
            "Artist - Album.rar": {"name": "Artist - Album", "kind": "album", "count": 9},
            "Ålbum.rar": {"name": "Ålbum", "kind": "album", "count": 9},
            # Master-list rows, by name and by an absolute key in the lists dir.
            os.path.join(self.tree.lists, "SomeBot-2026-08-01.zip"):
                {"name": "SomeBot-2026-08-01.zip", "kind": "file", "count": 91},
            os.path.join(self.tree.lists, "OldNick-2026-07-01.zip"):
                {"name": "OldNick-2026-07-01.zip", "kind": "file", "count": 40},
            # Junk the old table never showed, and values it read leniently.
            "junk/string": "nonsense",
            "junk/many": {"name": "Many", "kind": "file", "count": "many"},
            "junk/zero": {"name": "Zero", "kind": "file", "count": 0},
            "junk/negative": {"name": "Negative", "kind": "file", "count": -3},
            "junk/missing": {"name": "Missing", "kind": "file"},
            os.path.join("Flac", "lenient", "Text.flac"): {"name": "Text.flac", "kind": "file", "count": "7"},
            os.path.join("Flac", "lenient", "Float.flac"): {"name": "Float.flac", "kind": "file", "count": 2.9},
            os.path.join("Flac", "lenient", "Nameless.flac"): {"kind": "file", "count": 1},
        }
        old_save(self.path, self.fixture)

    def expected(self, migrate):
        counts = old_load(self.path)
        if migrate:
            counts = old_migrate_to_labels(counts, {"flac"}, "Flac")
        return old_prune(counts, os.path.abspath(self.tree.lists))

    def startup(self):
        """What oserve.startup() calls, in its order."""
        db.migrate_download_counts_to_labels()
        db.prune_list_artifact_download_counts()

    def assert_same_tables(self, expected):
        for kind in (None, "file", "album"):
            for limit in (3, 1000):
                with self.subTest(kind=kind, limit=limit):
                    self.assertEqual(db.top_downloads(limit=limit, kind=kind),
                                     old_ranking(expected, kind, limit))

    def test_the_whole_startup_gives_the_old_codes_numbers(self):
        expected = self.expected(migrate=True)
        before = self.json_bytes()

        self.startup()

        self.assert_same_tables(expected)
        self.assertEqual(db.load_download_counts()[os.path.join("Flac", "Artist", "Album",
                                                                "01 - Intro.flac")]["count"], 7)
        self.assertEqual(self.json_bytes(), before)

    def test_an_install_already_labelled_imports_and_prunes(self):
        """Most v1.13.x installs ran the label migration long ago and carry
        its marker, so their first start here is the import and the prune."""
        with io.open(os.path.join(os.path.dirname(self.path), ".download_counts_labelled"),
                     "w", encoding="utf-8") as handle:
            handle.write("ran\n")
        expected = self.expected(migrate=False)

        self.startup()

        self.assert_same_tables(expected)

    def test_a_second_start_changes_nothing(self):
        self.startup()
        first = db.load_download_counts()
        self.startup()
        self.assertEqual(db.load_download_counts(), first)

    def test_sends_after_the_upgrade_count_on_and_never_touch_the_json(self):
        before = self.json_bytes()
        stamp = os.stat(self.path).st_mtime_ns
        self.startup()
        key = os.path.join("Flac", "Artist", "Album", "02 - Song.flac")

        self.assertEqual(db.record_download(key, "02 - Song.flac", "file"), 13)
        self.assertEqual(db.record_download("new/one.flac", "one.flac", "file"), 1)

        self.assertEqual(self.json_bytes(), before)
        self.assertEqual(os.stat(self.path).st_mtime_ns, stamp)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))),
                         [".download_counts_labelled", "download_counts.db", "download_counts.json"])

    def test_the_old_version_still_reads_the_json_after_the_upgrade(self):
        """The downgrade: the file an older version reads is the one it left."""
        self.startup()
        db.record_download("new/one.flac", "one.flac", "file")
        self.assertEqual(old_load(self.path), self.fixture)

    def test_the_label_migration_waits_for_an_import_that_failed(self):
        """Migrating before the import would write its marker over rows the
        import brings in later, and they would stay unlabelled for good."""
        real = db._legacy_download_count_rows

        def failing(loaded):
            yield from ()
            raise RuntimeError("the import breaks")

        marker = os.path.join(os.path.dirname(self.path), ".download_counts_labelled")
        with mock.patch.object(db, "_legacy_download_count_rows", failing):
            self.assertEqual(db.migrate_download_counts_to_labels(), 0)
        self.assertFalse(os.path.exists(marker))
        self.assertIs(db._legacy_download_count_rows, real)

        self.startup()
        self.assertTrue(os.path.exists(marker))
        self.assert_same_tables(self.expected(migrate=True))

    def test_a_row_the_old_table_never_showed_starts_again_at_one(self):
        """Zero, negative and unreadable counts are left out of the import, so
        the next send of that key is its first - not -3 + 1. An install
        that is already labelled, so the junk keys stay where they are."""
        with io.open(os.path.join(os.path.dirname(self.path), ".download_counts_labelled"),
                     "w", encoding="utf-8") as handle:
            handle.write("ran\n")
        self.startup()
        self.assertEqual(db.record_download("junk/negative", "Negative", "file"), 1)
        self.assertEqual(db.record_download("junk/zero", "Zero", "file"), 1)
        self.assertEqual(db.record_download("junk/many", "Many", "file"), 1)
        self.assertEqual(db.record_download("junk/string", "String", "file"), 1)


# ---------------------------------------------------------------------------
# The import is one transaction.
# ---------------------------------------------------------------------------

KILL_MID_IMPORT = textwrap.dedent("""
    import os, sys
    import db
    db.DOWNLOAD_COUNTS_FILE = sys.argv[1]
    stop_after = int(sys.argv[2])
    real = db._legacy_download_count_rows

    def dying(loaded):
        for n, row in enumerate(real(loaded)):
            if n == stop_after:
                os._exit(17)
            yield row
        os._exit(17)        # every row is in; the marker is not

    db._legacy_download_count_rows = dying
    db.record_download("brand/new.flac", "new.flac", "file")
    os._exit(0)
""")


class TheImportIsOneTransaction(_CountsCase):

    def setUp(self):
        super().setUp()
        self.fixture = {f"Flac/A/{n:04d} - Track.flac": {"name": f"{n:04d} - Track.flac", "kind": "file",
                                                         "count": n % 7 + 1} for n in range(400)}
        self.fixture["Flac/A/Ünïcode.flac"] = {"name": "Ünïcode.flac", "kind": "file", "count": 3}
        self.fixture["junk"] = "not a row"
        old_save(self.path, self.fixture)
        self.wanted = {key: row["count"] for key, row in self.fixture.items() if isinstance(row, dict)}

    def kill_mid_import(self, stop_after):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env["PYTHONPATH"] = os.path.join(REPO_ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
        done = subprocess.run([sys.executable, "-c", KILL_MID_IMPORT, self.path, str(stop_after)],
                              cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, env=env)
        self.assertEqual(done.returncode, 17, done.stdout[-800:] + done.stderr[-800:])

    def test_a_kill_part_way_leaves_nothing_and_the_next_start_imports_exactly(self):
        for stop_after in (150, len(self.fixture)):
            with self.subTest(stop_after=stop_after):
                for leftover in (self.database, self.database + "-wal", self.database + "-shm"):
                    if os.path.exists(leftover):
                        os.remove(leftover)
                before = self.json_bytes()

                self.kill_mid_import(stop_after)

                self.assertTrue(os.path.isfile(self.database), "the child never reached the import")
                self.assertEqual(self.raw_rows(), ({}, False))
                self.assertEqual(self.json_bytes(), before)
                self.assertEqual({key: row["count"] for key, row in db.load_download_counts().items()},
                                 self.wanted)
                self.assertEqual(db.record_download("Flac/A/0001 - Track.flac", "", ""), 3)
                self.assertEqual(self.raw_rows()[0]["Flac/A/0001 - Track.flac"], 3)
                self.assertTrue(self.raw_rows()[1])

    def test_an_import_that_fails_loses_no_send_and_is_tried_again(self):
        real = db._legacy_download_count_rows

        def failing(loaded):
            for n, row in enumerate(real(loaded)):
                if n == 50:
                    raise RuntimeError("an import that breaks half way")
                yield row

        with mock.patch.object(db, "_legacy_download_count_rows", failing):
            self.assertEqual(db.record_download("Flac/A/0005 - Track.flac", "x", "file"), 1)
        self.assertEqual(self.raw_rows(), ({"Flac/A/0005 - Track.flac": 1}, False))

        counts = {key: row["count"] for key, row in db.load_download_counts().items()}

        self.assertEqual(counts["Flac/A/0005 - Track.flac"], self.wanted["Flac/A/0005 - Track.flac"] + 1)
        del counts["Flac/A/0005 - Track.flac"]
        self.assertEqual(counts, {k: v for k, v in self.wanted.items() if k != "Flac/A/0005 - Track.flac"})
        self.assertTrue(self.raw_rows()[1])

    def test_a_json_that_cannot_be_read_yet_is_imported_later(self):
        """A file that is there but cannot be opened (here: a directory in
        its place) is not "nothing to import": no marker, and the sends
        counted meanwhile are added to, not replaced."""
        os.replace(self.path, self.path + ".moved")
        os.mkdir(self.path)

        self.assertEqual(db.record_download("Flac/A/0002 - Track.flac", "x", "file"), 1)
        self.assertFalse(self.raw_rows()[1])

        os.rmdir(self.path)
        os.replace(self.path + ".moved", self.path)
        self.assertEqual(db.record_download("Flac/A/0002 - Track.flac", "x", "file"),
                         self.wanted["Flac/A/0002 - Track.flac"] + 2)

    def race(self, threads_n, send, patches):
        """Run `send` on threads_n threads at once; what db printed meanwhile."""
        said = []
        with contextlib.ExitStack() as stack:
            for target, name, value in patches:
                stack.enter_context(mock.patch.object(target, name, value))
            stack.enter_context(mock.patch.object(
                db, "print", create=True, new=lambda *a, **k: said.append(" ".join(map(str, a)))))
            threads = [threading.Thread(target=send) for _ in range(threads_n)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=90)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        return said

    def assert_imported_once_plus(self, key, sends, said):
        counts = {k: row["count"] for k, row in db.load_download_counts().items()}
        self.assertEqual(counts.pop(key), self.wanted[key] + sends)
        self.assertEqual(counts, {k: v for k, v in self.wanted.items() if k != key})
        # Exact counts, and no thread that gave up on its import and leaned on
        # the retry: the ones that waited found the marker.
        self.assertEqual([line for line in said if "Could not" in line], [])
        self.assertEqual(len([line for line in said if "moved into the database" in line]), 1)

    def test_concurrent_first_opens_import_once(self):
        """Several send threads finishing together on the first start after
        the upgrade, the way the bot runs them."""
        threads_n, key = 8, "Flac/A/0003 - Track.flac"
        start = threading.Barrier(threads_n, timeout=30)

        def send():
            start.wait()
            db.record_download(key, "x", "file")

        self.assert_imported_once_plus(key, threads_n, self.race(threads_n, send, []))

    def test_the_database_alone_imports_once_when_openers_race(self):
        """The same race with the process lock taken away, as a second
        process would meet it: the database is there, nothing is imported
        yet, and a barrier makes every opener pass the first marker check
        before any of them begins. BEGIN IMMEDIATE and the check inside it
        are all that is left between them.

        The database is created first, by the real code with the import held
        back: creating one file from several connections at once is a race
        the process lock exists to prevent, and not the one tested here."""
        with mock.patch.object(db, "_import_download_counts_once", lambda conn, json_path: None):
            with runtime.download_counts_lock:
                db._with_download_counts(lambda conn: None)
        self.assertEqual(self.raw_rows(), ({}, False))

        threads_n, key = 6, "Flac/A/0004 - Track.flac"
        together = threading.Barrier(threads_n, timeout=30)
        real = db._read_legacy_download_counts

        def read_together(json_path):
            together.wait()
            return real(json_path)

        said = self.race(threads_n, lambda: db.record_download(key, "x", "file"),
                         [(runtime, "download_counts_lock", contextlib.nullcontext()),
                          (db, "_read_legacy_download_counts", read_together)])
        self.assert_imported_once_plus(key, threads_n, said)


# ---------------------------------------------------------------------------
# A send.
# ---------------------------------------------------------------------------

class _DiskLockSpy:
    """Stands in for runtime.disk_lock and remembers being taken."""

    def __init__(self):
        self.taken = 0

    def acquire(self, *args, **kwargs):
        self.taken += 1
        return True

    def release(self):
        pass

    def locked(self):
        return False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        return False


class ASend(_CountsCase):

    def test_concurrent_sends_lose_nothing(self):
        threads_n, calls = 8, 25

        def sender(index):
            for call in range(calls):
                db.record_download("shared.flac", "shared.flac", "file")
                db.record_download(f"own/{index}.flac", f"{index}.flac", "file")

        threads = [threading.Thread(target=sender, args=(index,)) for index in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(thread.is_alive() for thread in threads))

        counts = db.load_download_counts()
        self.assertEqual(counts["shared.flac"]["count"], threads_n * calls)
        for index in range(threads_n):
            self.assertEqual(counts[f"own/{index}.flac"]["count"], calls)

    def test_nothing_here_takes_the_disk_lock(self):
        old_save(self.path, {"a": {"name": "A", "kind": "file", "count": 2}})
        spy = _DiskLockSpy()
        with mock.patch.object(db, "_disk_lock", spy), mock.patch.object(runtime, "disk_lock", spy):
            self.assertEqual(db.record_download("a", "A", "file"), 3)
            self.assertEqual(db.top_downloads()[0]["count"], 3)
            self.assertEqual(db.load_download_counts()["a"]["count"], 3)
            db.migrate_download_counts_to_labels()
            db.prune_list_artifact_download_counts()
        self.assertEqual(spy.taken, 0)

    def test_the_old_fallbacks_for_name_and_kind(self):
        self.assertIsNone(db.record_download("", "x", "file"))
        self.assertIsNone(db.record_download(None, "x", "file"))
        self.assertFalse(os.path.exists(self.database))

        self.assertEqual(db.record_download("k", None, None), 1)
        self.assertEqual(db.load_download_counts()["k"], {"name": "k", "kind": "file", "count": 1})
        self.assertEqual(db.record_download("k", "Shown", "album"), 2)
        self.assertEqual(db.record_download("k", "", ""), 3)
        self.assertEqual(db.load_download_counts()["k"], {"name": "Shown", "kind": "album", "count": 3})

    def test_a_reader_with_nothing_counted_creates_nothing(self):
        self.assertEqual(db.top_downloads(), [])
        self.assertEqual(db.load_download_counts(), {})
        self.assertFalse(os.path.exists(self.database))

    def test_the_first_send_creates_the_data_directory(self):
        db.DOWNLOAD_COUNTS_FILE = os.path.join(self.tree.root, "fresh", "download_counts.json")
        self.assertEqual(db.record_download("a", "A", "file"), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.tree.root, "fresh", "download_counts.db")))


# ---------------------------------------------------------------------------
# Where the database is.
# ---------------------------------------------------------------------------

class WhereTheDatabaseIs(_CountsCase):

    def test_beside_the_json_with_its_extension_replaced(self):
        self.assertEqual(db._download_counts_paths(), (self.database, self.path))

    def test_a_setting_that_names_a_database_is_the_database(self):
        target = os.path.join(self.tree.root, "data", "Counts.DB")
        db.DOWNLOAD_COUNTS_FILE = target

        self.assertEqual(db._download_counts_paths(), (target, None))
        self.assertEqual(db.record_download("a", "A", "file"), 1)
        self.assertEqual(db.record_download("a", "A", "file"), 2)
        self.assertEqual(sorted(os.listdir(os.path.dirname(target))), ["Counts.DB"])
        self.assertEqual(db.top_downloads(), [{"name": "A", "kind": "file", "count": 2}])

    def test_a_setting_without_an_extension(self):
        db.DOWNLOAD_COUNTS_FILE = os.path.join(self.tree.root, "data", "counts")
        self.assertEqual(db._download_counts_paths()[0], os.path.join(self.tree.root, "data", "counts.db"))


# ---------------------------------------------------------------------------
# A damaged database.
# ---------------------------------------------------------------------------

class ADamagedDatabase(_CountsCase):

    def setUp(self):
        super().setUp()
        old_save(self.path, {"a": {"name": "A", "kind": "file", "count": 5},
                             "b": {"name": "B", "kind": "album", "count": 2}})
        db.record_download("a", "A", "file")        # imports: a is 6
        db.record_download("c", "C", "file")

    def asides(self):
        """The moved-aside databases, without the sidecars that may go with
        them: whether SQLite left a -wal to move depends on the platform."""
        return [name for name in os.listdir(os.path.dirname(self.path))
                if name.startswith("download_counts.db.corrupt-")
                and not name.endswith(("-wal", "-shm"))]

    def test_garbage_is_moved_aside_and_the_json_imported_again(self):
        with io.open(self.database, "wb") as handle:
            handle.write(b"this is not a database at all" * 200)

        self.assertEqual(db.record_download("a", "A", "file"), 6)

        self.assertEqual(len(self.asides()), 1)
        self.assertEqual({k: row["count"] for k, row in db.load_download_counts().items()},
                         {"a": 6, "b": 2})

    def test_damage_further_in_is_found_by_the_reader_too(self):
        """The header and schema read fine; the table's pages do not."""
        conn = sqlite3.connect(self.database)
        conn.executemany("INSERT INTO download_counts VALUES (?, ?, 'file', 1)",
                         [(f"pad/{n:05d}/" + "x" * 200, f"n{n}") for n in range(2000)])
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        size = os.path.getsize(self.database)
        with io.open(self.database, "r+b") as handle:
            handle.seek(size // 3)
            handle.write(b"\xff" * (size // 3))

        self.assertEqual([row["name"] for row in db.top_downloads(kind=None, limit=2)], ["A", "B"])
        self.assertEqual(len(self.asides()), 1)

    def test_its_wal_and_shm_go_with_it(self):
        """SQLite removes its sidecars when the last connection closes; when
        it could not, a WAL left beside a fresh file would be replayed into
        it and carry the damage back. The same check transfer_log's own
        move-aside has."""
        bodies = (("", b"damaged"), ("-wal", b"stale frames"), ("-shm", b"index"))
        for suffix, body in bodies:
            with io.open(self.database + suffix, "wb") as handle:
                handle.write(body)

        db._move_download_counts_aside(self.database, "a test")

        aside = os.path.join(os.path.dirname(self.path), self.asides()[0])
        for suffix, body in bodies:
            self.assertFalse(os.path.exists(self.database + suffix), suffix)
            with io.open(aside + suffix, "rb") as handle:
                self.assertEqual(handle.read(), body)

    def test_the_json_still_untouched(self):
        before = self.json_bytes()
        with io.open(self.database, "wb") as handle:
            handle.write(b"garbage" * 100)
        db.record_download("a", "A", "file")
        self.assertEqual(self.json_bytes(), before)


# ---------------------------------------------------------------------------
# The ranking.
# ---------------------------------------------------------------------------

class TheRankingIsTheOldOne(_CountsCase):

    def test_thousands_of_awkward_rows(self):
        rng = random.Random(1133)
        stems = ["École", "école", "ECOLE", "Ωmega", "ωmega", "Straße", "STRASSE", "İstanbul",
                 "istanbul", "ǅemal", "zeta", "Zeta", "Ålbum", "album", "Song", "song", "日本", "Ünï"]
        kinds = ["file", "file", "album", None, "", "File", 5]
        counts_pool = [0, 1, 1, 1, 2, 2, 3, 3, 5, 8, -1, "4", "x", None, 2.7, True, 13]
        counts = {}
        for n in range(4000):
            key = rng.choice(stems) + "/" + rng.choice(["a", "B", "ç", "Ж"]) + f"/{n}.flac"
            name = rng.choice(stems) + rng.choice(["", " 1", " 2"])
            counts[key] = {"name": rng.choice([name, name, "", None]),
                           "kind": rng.choice(kinds), "count": rng.choice(counts_pool)}
        counts["broken"] = "not a row"
        counts["also broken"] = ["a", "list"]
        old_save(self.path, counts)
        as_read = old_load(self.path)

        for kind in (None, "file", "album", "File", "", "nothing", 5, "5"):
            for limit in (0, 1, 3, 10, 57, 5000, "x", -2, None):
                with self.subTest(kind=kind, limit=limit):
                    self.assertEqual(db.top_downloads(limit=limit, kind=kind),
                                     old_ranking(as_read, kind, limit))

    def test_a_poll_reads_the_rows_it_shows_not_the_table(self):
        """The reason for the (kind, count) index: with no ties at the
        limit-th count, ten rows come back for a top ten, of 500."""
        old_save(self.path, {f"k{n:04d}": {"name": f"n{n}", "kind": "file", "count": n + 1}
                             for n in range(500)})
        db.top_downloads()                      # the import, outside the count
        fetched = []
        real = db._read_download_counts

        def counting(work):
            rows = real(work)
            fetched.append(len(rows))
            return rows

        with mock.patch.object(db, "_read_download_counts", counting):
            top = db.top_downloads(limit=10, kind="file")
            everything = db.top_downloads(limit=10, kind=None)

        self.assertEqual([row["count"] for row in top], list(range(500, 490, -1)))
        self.assertEqual(everything, top)
        self.assertEqual(fetched, [10, 10])

    def test_a_send_after_the_import_ties_in_key_order(self):
        """Equal count, equal name.lower(): the JSON version wrote its file in
        key order, so "a" came before "b" however late "a" was first sent."""
        old_save(self.path, {"b": {"name": "Same", "kind": "file", "count": 1}})
        db.top_downloads()                      # imports "b"
        db.record_download("a", "same", "file")

        self.assertEqual([row["name"] for row in db.top_downloads()], ["same", "Same"])

    def test_a_key_sqlite_cannot_store_does_not_stop_the_rest(self):
        """A lone surrogate, which the JSON version could never have saved
        either: only a hand edit puts one there."""
        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"bad\\ud800": {"name": "Bad", "kind": "file", "count": 3},'
                         ' "fine": {"name": "Fine", "kind": "file", "count": 2}}')
        self.assertIn("\ud800", list(old_load(self.path))[0])

        self.assertEqual(db.top_downloads(), [{"name": "Fine", "kind": "file", "count": 2}])

    def test_a_count_too_big_for_sqlite_does_not_stop_the_rest(self):
        old_save(self.path, {"huge": {"name": "Huge", "kind": "file", "count": 10 ** 20},
                             "fine": {"name": "Fine", "kind": "file", "count": 2}})
        self.assertEqual(db.top_downloads(), [{"name": "Fine", "kind": "file", "count": 2}])


if __name__ == "__main__":
    unittest.main()
