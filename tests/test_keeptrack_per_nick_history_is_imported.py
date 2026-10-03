"""KeepTrack's per-nick history (KTData.txt) comes into the transfer record (#1064).

KTData.txt holds one line per nick and direction: Sent/Received, a host mask,
the nick, files and bytes. The nick and the totals come across, summed per
lower-cased nick; the host never does. The figures sit in the record's
`imported` table, so the per-nick rankings and summaries add them for all
time and leave them out of a period, and forgetting a nick forgets them too.
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests import support  # noqa: F401  (path setup)

import omenserve_import  # noqa: E402
import transfer_log  # noqa: E402
import webserver  # noqa: E402

REPO_ROOT = support.REPO_ROOT
TAB = chr(9)
HOST = "secret-host.example.org"


def line(*fields):
    return TAB.join(str(f) for f in fields)


KTDATA = "\n".join([
    line("Sent", "*!*someuser@" + HOST, "SomeUser", 412, 18234567890),
    line("Sent", "*!*someuser@other.example.org", "someuser", 8, 100),
    line("Sent", "*!*erin@example.net", "Erin", 3, 300),
    line("Received", "*!*bot@example.net", "Some[Bot]", 57, 2345678901),
    line("Sent", "*!*x@example.net", "bad nick", 1, 1),
    "not a line at all",
    line("Uploaded", "*!*x@example.net", "Nick", 1, 1),
    line("Sent", "*!*x@example.net", "Nick", "many", 1),
])


class TheFile(unittest.TestCase):
    def test_lines_are_summed_per_nick_and_the_host_is_not_kept(self):
        parsed = omenserve_import.read_ktdata(KTDATA)
        self.assertEqual(parsed["rows"], [("received", "some[bot]", 57, 2345678901),
                                          ("sent", "erin", 3, 300),
                                          ("sent", "someuser", 420, 18234567990)])
        self.assertNotIn(HOST, json.dumps(parsed))

    def test_bad_lines_are_counted_by_why(self):
        self.assertEqual(omenserve_import.read_ktdata(KTDATA)["skipped"], {
            "not a nick": 1, "not five tab-separated fields": 1,
            "neither Sent nor Received": 1, "a count that is not a whole number": 1})

    def test_out_of_range_is_skipped(self):
        parsed = omenserve_import.read_ktdata(line("Sent", "h", "Nick", -1, 5), max_files=10, max_bytes=10)
        self.assertEqual(parsed["skipped"], {"a count out of range": 1})

    def test_a_nick_whose_lines_sum_past_the_limit_is_skipped(self):
        """#1064 review: each line was checked, their sum per nick was not."""
        text = "\n".join([line("Sent", "h", "Bob", 6, 1), line("Sent", "h", "bob", 6, 1),
                          line("Sent", "h", "Amy", 3, 1)])
        parsed = omenserve_import.read_ktdata(text, max_files=10, max_bytes=10)
        self.assertEqual(parsed["rows"], [("sent", "amy", 3, 1)])
        self.assertEqual(parsed["skipped"], {"a count out of range": 1})

    def test_the_limits_leave_room_for_the_records_own_rows(self):
        """A nick at the limit, summed in SQL with its recorded sends, must not
        overflow: that emptied the all-time ranking (#1064 review)."""
        parsed = omenserve_import.read_ktdata(line("Sent", "h", "Bob", 2 ** 63 - 1, 1))
        self.assertEqual(parsed["rows"], [])
        self.assertLessEqual(1 << 40, 2 ** 63 // 1000)   # a thousand such nicks still fit one SUM


class Case(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="dccore-ktdata-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "transfers.db")
        self.set_config(TRANSFER_LOG_FILE=self.path)

    def raw(self):
        data = b""
        for name in (self.path, self.path + "-wal"):
            if os.path.exists(name):
                with open(name, "rb") as handle:
                    data += handle.read()
        return data


class TheImport(Case):
    def test_a_nick_a_new_import_drops_is_gone_from_the_file(self):
        """A re-import replaces the old rows, and those name nicks: the file is
        rebuilt after, as a forget does (#1099), or a nick the new file no
        longer holds stays readable in the free space of pages that still
        hold other rows (#1082)."""
        import sqlite3
        # Rows written as an older version or another SQLite build would:
        # freed without being zeroed. The dropped nick's rows sit among
        # another source's, so half of them deleted that way leave copies in
        # pages that stay live - which only a rebuild removes (as in #1099).
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA secure_delete = OFF")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(transfer_log._SCHEMA)
        with conn:
            for n in range(1500):
                if n % 25 == 0:
                    row = ("keeptrack", "droppedonreimport")
                else:
                    row = ("othersource", f"regular{n}")
                conn.execute("INSERT INTO imported (source, direction, nick, files, bytes, since, imported_at)"
                             " VALUES (?, 'sent', ?, 1, 100, NULL, 1)", row)
        with conn:
            conn.execute("DELETE FROM imported WHERE source = 'keeptrack' AND rowid % 2 = 1")
        conn.close()
        self.assertGreater(self.raw().count(b"droppedonreimport"), 40)

        self.assertEqual(transfer_log.import_nicks("keeptrack", [("sent", "kept", 1, 10)]), 1)

        self.assertEqual(self.raw().count(b"droppedonreimport"), 0)
        self.assertIn(b"regular1499", self.raw())

    def test_a_nick_at_the_limit_still_ranks_beside_its_own_sends(self):
        biggest = (1 << 40, 1 << 50)
        status, result = webserver.apply_ktdata_import(line("Sent", "h", "Bob", *biggest))
        self.assertEqual(status, 200, result)
        transfer_log.record_sent("file", "k", "A.flac", 10, 10, 1.0, 10, 1.0, nick="Bob")
        self.assertEqual(transfer_log.top_nicks(), [("bob", (1 << 40) + 1, (1 << 50) + 10)])

    def test_the_preview_says_what_would_come_across_and_writes_nothing(self):
        status, preview = webserver.build_ktdata_preview(KTDATA)
        self.assertEqual(status, 200, preview)
        self.assertEqual((preview["figures"]["sent"]["nicks"], preview["figures"]["sent"]["files"]), (2, 423))
        self.assertEqual(preview["figures"]["sent"]["top"][0], {"nick": "someuser", "files": 420, "bytes": 18234567990})
        self.assertEqual(preview["figures"]["received"]["nicks"], 1)
        self.assertEqual(sum(preview["skipped"].values()), 4)
        self.assertEqual(preview["replaces"], {})
        self.assertEqual(transfer_log.top_nicks(), [])

    def test_it_lands_and_the_rankings_add_it_for_all_time_only(self):
        transfer_log.record_sent("file", "k", "A.flac", 10, 10, 1.0, 10, 1.0, nick="Erin")
        status, result = webserver.apply_ktdata_import(KTDATA)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["imported"], 3)
        self.assertEqual(transfer_log.top_nicks(), [("someuser", 420, 18234567990), ("erin", 4, 310)])
        self.assertEqual(transfer_log.top_nicks(since=1), [("erin", 1, 10)])
        self.assertEqual(transfer_log.top_nicks(direction="received"), [("some[bot]", 57, 2345678901)])
        summary = transfer_log.nick_summary("Erin")
        self.assertEqual((summary["files_sent"], summary["bytes_sent"]), (4, 310))
        self.assertEqual(transfer_log.nick_summary("Erin", since=1)["files_sent"], 1)

    def test_the_host_is_nowhere_in_the_file(self):
        webserver.apply_ktdata_import(KTDATA)
        self.assertNotIn(HOST.encode(), self.raw())

    def test_importing_again_replaces_it(self):
        webserver.apply_ktdata_import(KTDATA)
        status, preview = webserver.build_ktdata_preview(KTDATA)
        self.assertEqual(preview["replaces"], {"sent": 2, "received": 1})
        webserver.apply_ktdata_import(line("Sent", "h", "Erin", 1, 1))
        self.assertEqual(transfer_log.top_nicks(), [("erin", 1, 1)])

    def test_it_keeps_keeptracks_start_date_when_the_totals_brought_one(self):
        webserver.apply_stats_import({"received_files": 7, "received_bytes": 700, "received_since": "2002-02-03"})
        webserver.apply_ktdata_import(KTDATA)
        import sqlite3
        conn = sqlite3.connect(self.path)
        try:
            dates = {row[0] for row in conn.execute("SELECT since FROM imported WHERE nick IS NOT NULL")}
        finally:
            conn.close()
        self.assertEqual(dates, {"2002-02-03"})

    def test_forgetting_a_nick_forgets_its_imported_figures_too(self):
        webserver.apply_ktdata_import(KTDATA)
        transfer_log.forget_nick("SomeUser")
        self.assertEqual([n for n, _f, _b in transfer_log.top_nicks()], ["erin"])
        self.assertNotIn(b"someuser", self.raw())

    def test_with_the_record_off_nothing_is_read(self):
        self.set_config(TRANSFER_LOG_FILE="")
        status, result = webserver.build_ktdata_preview(KTDATA)
        self.assertEqual(status, 409)
        self.assertIn("transfer record is off", result["error"])

    def test_a_file_far_too_big_is_refused(self):
        status, _result = webserver.build_ktdata_preview("x" * (webserver.MAX_KTDATA_CHARS + 1))
        self.assertEqual(status, 400)

    def test_a_file_with_nothing_usable_is_refused(self):
        status, result = webserver.apply_ktdata_import("not a line at all")
        self.assertEqual(status, 400)


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class TheRoutes(Case):
    def test_preview_then_import_over_http(self):
        import adminchat
        from tests.test_webserver import WEBUI_TEST_PASSWORD, log_in_test_client
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(WEBUI_TEST_PASSWORD, iterations=1000))
        client = webserver.create_app().test_client()
        log_in_test_client(client)
        preview = client.post("/api/stats/import-ktdata/preview", json={"text": KTDATA})
        self.assertEqual(preview.status_code, 200)
        done = client.post("/api/stats/import-ktdata", json={"text": KTDATA})
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.get_json()["imported"], 3)


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function fn(signature) {
  const start = src.indexOf(signature);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") { depth++; }
    else if (src[i] === "}") { depth--; if (depth === 0) { break; } }
  }
  return src.slice(start, i + 1);
}
const lines = [];
const el = {
  ktdataPreview: { innerHTML: "", hidden: true, appendChild: function (n) { lines.push(n.textContent); } },
  ktdataConfirm: { hidden: true }
};
const document = { createElement: function () { return { textContent: "" }; } };
const t = function (key) { return key + " {sentNicks} {sentFiles} {receivedNicks} {receivedFiles} {count} {reasons}"; };
const make = new Function("el", "document", "t",
  fn("function ktdataLine(") + "\n" + fn("function renderKTDataPreview(") + "\nreturn renderKTDataPreview;");
const render = make(el, document, t);
render({ figures: { sent: { nicks: 2, files: 423, top: [{ nick: "someuser", files: 420 }] },
                    received: { nicks: 0, files: 0, top: [] } },
         skipped: { "not a nick": 1 }, replaces: { sent: 2 } });
const first = { lines: lines.slice(), confirmHidden: el.ktdataConfirm.hidden };
lines.length = 0;
render({ figures: { sent: { nicks: 0 }, received: { nicks: 0 } }, skipped: {}, replaces: {} });
console.log(JSON.stringify({ first: first, empty: { lines: lines, confirmHidden: el.ktdataConfirm.hidden } }));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class ThePreviewOnThePage(unittest.TestCase):
    def test_it_lists_the_figures_and_offers_the_import_only_when_there_is_something(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        result = json.loads(done.stdout.decode("utf-8"))
        first = result["first"]
        self.assertTrue(first["lines"][0].startswith("stats.ktdataSummary 2 423 0 0"), first["lines"])
        self.assertTrue(any(l.startswith("stats.ktdataTopSent") and "someuser (420)" in l for l in first["lines"]))
        self.assertTrue(any(l.startswith("stats.ktdataSkipped") and "not a nick (1)" in l for l in first["lines"]))
        self.assertTrue(any(l.startswith("stats.ktdataReplaces") for l in first["lines"]))
        self.assertFalse(first["confirmHidden"])
        self.assertTrue(result["empty"]["confirmHidden"], "nothing to import offers no import")


if __name__ == "__main__":
    unittest.main()
