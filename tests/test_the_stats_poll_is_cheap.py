"""The Stats and Live Transfers polls do not re-read whole files (#1123).

/api/stats ranked download_counts.json twice per poll - each time parsing the
whole file under db's disk lock, which the send threads' record_download()
needs too - and counted every row of the RAR list. About 5 s a poll on a big
bot, every few seconds, and since #1118 it is Live Transfers that polls it,
a page that shows neither.

Now Live Transfers asks for ?parts=transfer only; the ranking is built once per
change of the counts file, parsed outside the lock; and the RAR count is kept
per list file until that file changes.

#1133 then moved the counts into SQLite, where the ranking is an indexed query
and a send is one row, so the ranking cache and its file signature went: the
tests of what a caller sees stay, and the two that pinned the cache's
invalidation now pin that the JSON is read once, at the import.
"""

import io
import json
import os
import random
import unittest
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import db  # noqa: E402
import webserver  # noqa: E402

REPO_ROOT = support.REPO_ROOT


class CountsCase(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.path = os.path.join(self.tree.root, "download_counts.json")
        patch = mock.patch.object(db, "DOWNLOAD_COUNTS_FILE", self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, counts):
        """Sorted by key, as the JSON version always wrote the file: rows equal
        in count and in name.lower() came out in file order, which is key order,
        and the database keeps exactly that."""
        with io.open(self.path, "w", encoding="utf-8") as handle:
            json.dump(counts, handle, sort_keys=True)

    def reference(self, counts, kind, limit):
        """The ranking as top_downloads() computed it before #1123."""
        rows = []
        for key, row in counts.items():
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
            rows.append({"name": str(row.get("name") or key), "kind": row_kind, "count": count})
        rows.sort(key=lambda entry: (-entry["count"], entry["name"].lower()))
        return rows[:limit]


class TheRanking(CountsCase):
    def test_the_same_answer_as_before(self):
        rng = random.Random(1123)
        counts = {}
        for n in range(3000):
            counts[f"k{n}"] = {"name": rng.choice(["Song", "song", "Álbum", "zeta", "Beta"]) + f" {n % 97}",
                               "kind": rng.choice(["file", "file", "album", None]),
                               "count": rng.choice([0, 1, 1, 2, 3, 5, 8, -1, "7", "x"])}
        counts["broken"] = "not a row"
        self.write(counts)
        with io.open(self.path, encoding="utf-8") as handle:
            counts = json.load(handle)      # in the order the old code read them: the file's
        for kind in (None, "file", "album"):
            for limit in (0, 1, 10, 50, "x"):
                with self.subTest(kind=kind, limit=limit):
                    expected = self.reference(counts, kind, 10 if limit == "x" else limit)
                    self.assertEqual(db.top_downloads(limit=limit, kind=kind), expected)

    def test_a_poll_with_nothing_changed_reads_nothing(self):
        self.write({"a": {"name": "A", "kind": "file", "count": 3}})
        db.top_downloads(kind="file")
        with mock.patch.object(db.json, "loads", side_effect=AssertionError("parsed again")):
            self.assertEqual(db.top_downloads(kind="file")[0]["name"], "A")
            self.assertEqual(db.top_downloads(kind="album"), [])

    def test_a_completed_send_is_seen_at_once(self):
        self.write({"a": {"name": "A", "kind": "file", "count": 3}})
        self.assertEqual(db.top_downloads(kind="file")[0]["count"], 3)
        db.record_download("a", "A", "file")
        self.assertEqual(db.top_downloads(kind="file")[0]["count"], 4)
        db.record_download("b", "B", "file")
        self.assertEqual([r["name"] for r in db.top_downloads(kind="file")], ["A", "B"])

    def test_a_file_replaced_after_the_import_is_not_read_again(self):
        """Since #1133 the JSON is imported once and the database answers from
        then on, so a JSON put back later - by a downgrade and a re-upgrade,
        say - is not read again. The settings help says so."""
        self.write({"a": {"name": "A", "kind": "file", "count": 3}})
        db.top_downloads()
        replacement = self.path + ".new"
        with io.open(replacement, "w", encoding="utf-8") as handle:
            json.dump({"z": {"name": "Z", "kind": "file", "count": 9}}, handle)
        os.replace(replacement, self.path)
        self.assertEqual([r["name"] for r in db.top_downloads()], ["A"])

    def test_the_parse_runs_outside_the_disk_lock(self):
        self.write({"a": {"name": "A", "kind": "file", "count": 3}})
        real = json.loads
        held = []

        def loads(text, *args, **kwargs):
            held.append(db._disk_lock.locked())
            return real(text, *args, **kwargs)
        with mock.patch.object(db.json, "loads", loads):
            db.top_downloads()
        self.assertEqual(held, [False])

    def test_a_caller_cannot_edit_the_cached_ranking(self):
        self.write({"a": {"name": "A", "kind": "file", "count": 3}})
        db.top_downloads()[0]["count"] = 999
        self.assertEqual(db.top_downloads()[0]["count"], 3)

    def test_no_file_is_no_rows(self):
        self.assertEqual(db.top_downloads(), [])


class TheRarCount(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, LIST_BASE_NAME="DCCore")
        webserver._rar_counts.clear()
        self.addCleanup(webserver._rar_counts.clear)

    def rar_list(self, rows, name="DCCore-RAR-2026.10.03.txt"):
        path = os.path.join(self.tree.lists, name)
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as handle:
            handle.write("three\nlines of\nheader\n")
            for n in range(rows):
                handle.write(f"!DCCore !rar D:\\MEDIA\\Music\\Album {n}\\\n")
        os.replace(tmp, path)   # as update_list publishes
        return path

    def test_counted_once_until_the_list_changes(self):
        self.rar_list(5)
        self.assertEqual(webserver.count_rar_album_folders(), 5)
        with mock.patch("io.open", side_effect=AssertionError("read again")):
            self.assertEqual(webserver.count_rar_album_folders(), 5)
        self.rar_list(7)
        self.assertEqual(webserver.count_rar_album_folders(), 7)

    def test_no_list_is_none_not_zero(self):
        self.assertIsNone(webserver.count_rar_album_folders())


class ThePayloadInParts(support.DCCoreTestCase):
    """In the redirected sandbox: the whole payload reads the download
    counts, whose first read imports a download_counts.json it finds into a
    new database beside it (#1133) - which on a checkout run outside the
    sandbox is the developer's own data/."""

    def test_transfer_alone_reads_no_file(self):
        with mock.patch.object(db, "top_downloads", side_effect=AssertionError("ranked")), \
                mock.patch.object(webserver, "build_library_payload", side_effect=AssertionError("counted")), \
                mock.patch.object(db, "load_advanced_stats_rolled", side_effect=AssertionError("read stats.txt")):
            payload = webserver.build_stats_payload(["transfer"])
        self.assertEqual(sorted(payload), ["transfer", "version"])
        self.assertIn("speed_now_text", payload["transfer"])

    def test_no_parts_is_everything_as_before(self):
        payload = webserver.build_stats_payload()
        self.assertEqual(sorted(payload), ["library", "sent", "top", "transfer", "version"])


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class TheRoute(support.DCCoreTestCase):
    def client(self):
        import adminchat
        from tests.test_webserver import WEBUI_TEST_PASSWORD, log_in_test_client
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(WEBUI_TEST_PASSWORD, iterations=1000))
        client = webserver.create_app().test_client()
        log_in_test_client(client)
        return client

    def test_parts(self):
        client = self.client()
        self.assertEqual(sorted(client.get("/api/stats?parts=transfer").get_json()), ["transfer", "version"])
        self.assertEqual(sorted(client.get("/api/stats").get_json()), ["library", "sent", "top", "transfer", "version"])
        for bad in ("bogus", "", "transfer,bogus"):
            with self.subTest(parts=bad):
                self.assertEqual(client.get("/api/stats?parts=" + bad).status_code, 400)


class ThePage(unittest.TestCase):
    def js(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            return handle.read()

    def test_live_transfers_polls_only_its_own_figures(self):
        js = self.js()
        self.assertIn('if (name === "live") { loadLive(); loadQueue(); }', js)
        self.assertIn('if (state.active === "live") { loadLive(); }', js)
        body = js.split("  function loadLive() {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn('fetchJson("/api/stats?parts=transfer")', body)
        self.assertIn("renderTransfer(data.transfer);", body)
        self.assertNotIn("lastStats", body)

    def test_stats_still_asks_for_everything(self):
        self.assertIn('if (name === "stats") { loadStats(); loadRecord(); }', self.js())


if __name__ == "__main__":
    unittest.main()
