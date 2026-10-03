"""The transfer record is shown on the Stats page, and a nick can be forgotten (#1102).

The record (#1068) has been written since #1069, but nothing read it. The
Stats page now shows it for a chosen period - the totals, the files sent most
and the nicks sent to and received from most - looks up one nick, forgets one
nick or everyone, and exports it as CSV. Nicks and file names are other
people's text: the page sets them as text, and the export makes a cell that a
spreadsheet would run as a formula plain text again.
"""

import contextlib
import csv
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from tests import support

import defaults as config  # noqa: E402
import transfer_log  # noqa: E402
import webserver  # noqa: E402

REPO_ROOT = support.REPO_ROOT
DAY = 86400


def row(direction, nick, name, size, age_days=0, kind=transfer_log.KIND_FILE, seconds=10, waited=30):
    """A finished transfer, ended `age_days` ago."""
    ended = int(time.time() - age_days * DAY)
    speed = int(size / seconds) if seconds else None
    assert transfer_log._record((direction, nick, kind, ended, name.lower(), name, size, size,
                                 seconds, speed, waited))


class Case(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        row(transfer_log.SENT, "listener", "Song One.flac", 4000)
        row(transfer_log.SENT, "listener", "Song Two.flac", 6000)
        row(transfer_log.SENT, "listener", "Song One.flac", 4000, age_days=3)
        row(transfer_log.SENT, "otherone", "Song One.flac", 4000, age_days=40)
        row(transfer_log.SENT, "otherone", "list", 100, kind=transfer_log.KIND_LIST)
        row(transfer_log.RECEIVED, "peerbot", "Album.rar", 90000, age_days=2)


class ThePayload(Case):
    def test_all_time_counts_every_row(self):
        status, data = webserver.build_record_payload("all")
        self.assertEqual(status, 200)
        s = data["summary"]
        self.assertEqual((s["files_sent"], s["lists_sent"], s["files_received"]), (4, 1, 1))
        self.assertEqual(s["bytes_sent"], 18000)
        self.assertEqual(s["bytes_sent_text"], "17.6KB")
        self.assertEqual(s["queue_wait_text"], "30 Sec")
        self.assertEqual(data["top_files"][0], {"name": "Song One.flac", "count": 3})
        self.assertEqual([r["nick"] for r in data["top_sent"]], ["listener", "otherone"])
        self.assertEqual(data["top_received"][0]["nick"], "peerbot")
        self.assertFalse(data["includes_imported"])

    def test_a_period_leaves_out_what_is_older(self):
        expected = {"day": (2, 0), "week": (3, 1), "month": (3, 1), "all": (4, 1)}
        for period, (sent, received) in expected.items():
            with self.subTest(period=period):
                _status, data = webserver.build_record_payload(period)
                self.assertEqual((data["summary"]["files_sent"], data["summary"]["files_received"]),
                                 (sent, received))

    def test_an_unknown_period_is_refused(self):
        status, data = webserver.build_record_payload("forever")
        self.assertEqual(status, 400)
        self.assertIn("day, week, month or all", data["error"])

    def test_a_per_nick_import_alone_is_said_too(self):
        """#1102 review: the note looked for the bot's own totals only, while
        the nick tables already counted a per-nick import."""
        transfer_log.import_nicks("keeptrack", [("sent", "keptnick", 5000, 10 ** 9)])
        _status, data = webserver.build_record_payload("all")
        self.assertEqual(data["top_sent"][0]["nick"], "keptnick")
        self.assertTrue(data["includes_imported"])
        self.assertFalse(webserver.build_record_payload("day")[1]["includes_imported"])

    def test_imported_totals_count_for_all_time_only_and_it_is_said(self):
        transfer_log.import_totals("keeptrack", transfer_log.SENT, 10, 1000)
        _status, everything = webserver.build_record_payload("all")
        _status, week = webserver.build_record_payload("week")
        self.assertEqual(everything["summary"]["files_sent"], 14)
        self.assertTrue(everything["includes_imported"])
        self.assertEqual(week["summary"]["files_sent"], 3)
        self.assertFalse(week["includes_imported"])

    def test_a_long_wait_reads_as_minutes(self):
        self.assertEqual(webserver._wait_text(30), "30 Sec")
        self.assertEqual(webserver._wait_text(3 * 3600 + 120), "3 Hrs and 2 Min")
        self.assertEqual(webserver._wait_text(None), "")


class TheStatsPageFollowsOnePeriod(Case):
    """#1117: the Sent cards, Most downloaded files and Most downloaded albums
    all answer for the period chosen."""

    def test_a_period_gives_files_and_albums_apart(self):
        row(transfer_log.SENT, "listener", "Some Album", 50000, kind=transfer_log.KIND_ALBUM)
        row(transfer_log.SENT, "listener", "Some Album", 50000, kind=transfer_log.KIND_ALBUM, age_days=1)
        _status, data = webserver.build_record_payload("week")
        self.assertEqual([r["name"] for r in data["top_files"]], ["Song One.flac", "Song Two.flac"])
        self.assertEqual(data["top_albums"], [{"name": "Some Album", "count": 2}])
        self.assertTrue(data["albums_enabled"])

    def test_top_files_can_keep_one_kind(self):
        row(transfer_log.SENT, "listener", "Some Album", 50000, kind=transfer_log.KIND_ALBUM)
        self.assertEqual(transfer_log.top_files(10, None, transfer_log.KIND_ALBUM), [("Some Album", 1)])
        self.assertNotIn(("Some Album", 1), transfer_log.top_files(10, None, transfer_log.KIND_FILE))
        mixed = [name for name, _ in transfer_log.top_files(10)]
        self.assertIn("Some Album", mixed)

    def test_all_time_most_downloaded_is_the_count_file_when_it_has_rows(self):
        import db
        db.record_download("old/Old Song.flac", "Old Song.flac", "file")
        _status, everything = webserver.build_record_payload("all")
        self.assertEqual(everything["top_files"], [{"name": "Old Song.flac", "count": 1}])
        _status, week = webserver.build_record_payload("week")
        self.assertEqual(week["top_files"][0]["name"], "Song One.flac")

    def test_all_time_sent_is_never_below_the_lifetime_counter(self):
        import db
        db.set_lifetime_totals(total_files=500, total_bytes=10 ** 9)
        _status, everything = webserver.build_record_payload("all")
        self.assertEqual(everything["summary"]["files_sent"], 500)
        self.assertEqual(everything["summary"]["bytes_sent"], 10 ** 9)
        _status, week = webserver.build_record_payload("week")
        self.assertEqual(week["summary"]["files_sent"], 3)


class OneNick(Case):
    def test_its_figures_any_case(self):
        status, data = webserver.build_record_nick_payload("  LISTENER ", "all")
        self.assertEqual(status, 200)
        self.assertEqual(data["nick"], "listener")
        self.assertTrue(data["found"])
        self.assertEqual((data["figures"]["files_sent"], data["figures"]["bytes_sent"]), (3, 14000))

    def test_not_in_this_period(self):
        _status, data = webserver.build_record_nick_payload("otherone", "week")
        self.assertTrue(data["found"], "its list was sent today")
        _status, data = webserver.build_record_nick_payload("nobody", "all")
        self.assertFalse(data["found"])

    def test_a_blank_nick_is_refused(self):
        self.assertEqual(webserver.build_record_nick_payload("   ")[0], 400)


class Forgetting(Case):
    def test_one_nick_goes_and_the_rest_stay(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status, data = webserver.apply_record_forget({"nick": "Listener"})
        self.assertEqual((status, data["removed"]), (200, 3))
        self.assertNotIn("listener", out.getvalue().lower(), "the log keeps no forgotten nick")
        _status, after = webserver.build_record_payload("all")
        self.assertEqual([r["nick"] for r in after["top_sent"]], ["otherone"])
        self.assertEqual(after["summary"]["files_received"], 1)

    def test_a_blank_nick_never_wipes_the_record(self):
        for body in ({}, {"nick": ""}, {"nick": "  "}, {"everyone": "true"}, {"everyone": 1}):
            with self.subTest(body=body):
                status, _data = webserver.apply_record_forget(body)
                self.assertEqual(status, 400)
        self.assertEqual(webserver.build_record_payload("all")[1]["summary"]["files_sent"], 4)

    def test_everyone_empties_it(self):
        with contextlib.redirect_stdout(io.StringIO()):
            status, data = webserver.apply_record_forget({"everyone": True})
        self.assertEqual((status, data["removed"]), (200, 6))
        s = webserver.build_record_payload("all")[1]["summary"]
        self.assertEqual((s["files_sent"], s["files_received"]), (0, 0))


class TheExport(Case):
    def read(self, since=None):
        return list(csv.reader(io.StringIO("".join(webserver.record_csv_lines(since)))))

    def test_a_header_and_every_row_in_the_order_written(self):
        rows = self.read()
        self.assertEqual(rows[0], ["time", "direction", "nick", "kind", "name", "size", "bytes",
                                   "seconds", "speed", "waited"])
        self.assertEqual(len(rows), 7)
        self.assertEqual([r[2] for r in rows[1:]],
                         ["listener", "listener", "listener", "otherone", "otherone", "peerbot"])
        self.assertRegex(rows[1][0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_it_is_read_in_chunks_and_holds_no_reader_between_them(self):
        """#1102 review: one cursor open for the whole export kept a snapshot
        as long as the download ran, and a forget could not empty the WAL."""
        import sqlite3
        from unittest import mock
        with mock.patch.object(transfer_log, "EXPORT_CHUNK", 2):
            rows = transfer_log.iter_rows()
            first = next(rows)
            # Mid-export, as a stalled download would leave it: nothing reads now.
            with contextlib.redirect_stdout(io.StringIO()):
                transfer_log.forget_nick("otherone")
            conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
            try:
                busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            finally:
                conn.close()
            rest = list(rows)
        self.assertEqual(busy, 0)
        self.assertEqual(first[2], "listener")
        self.assertEqual([r[2] for r in rest], ["listener", "listener", "peerbot"])

    def test_since_leaves_out_what_is_older(self):
        self.assertEqual(len(self.read(int(time.time()) - DAY)), 4)

    def test_a_cell_a_spreadsheet_would_run_is_text(self):
        for name in ("=HYPERLINK(1)", "+1+1", "-2+3", "@SUM(1)", "\tTab"):
            row(transfer_log.RECEIVED, "peerbot", name, 10)
        names = [r[4] for r in self.read()[7:]]
        self.assertEqual(names, ["'=HYPERLINK(1)", "'+1+1", "'-2+3", "'@SUM(1)", "'\tTab"])

    def test_iter_rows_lets_its_connection_go(self):
        """Read to the end, the file is free: on Windows an open one would block moving it."""
        list(transfer_log.iter_rows())
        os.replace(config.TRANSFER_LOG_FILE, config.TRANSFER_LOG_FILE + ".moved")


class WhenTheRecordIsOff(Case):
    def test_every_part_says_so(self):
        self.set_config(TRANSFER_LOG_FILE="")
        status, data = webserver.build_record_payload("all")
        self.assertEqual((status, data["enabled"]), (200, False))
        self.assertEqual(webserver.build_record_nick_payload("listener")[0], 409)
        self.assertEqual(webserver.apply_record_forget({"everyone": True})[0], 409)


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class TheRoutes(Case):
    def client(self):
        import adminchat
        from tests.test_webserver import WEBUI_TEST_PASSWORD, log_in_test_client
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(WEBUI_TEST_PASSWORD, iterations=1000))
        client = webserver.create_app().test_client()
        log_in_test_client(client)
        return client

    def test_over_http(self):
        client = self.client()
        got = client.get("/api/stats/record?period=week")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.get_json()["summary"]["files_sent"], 3)
        self.assertEqual(client.get("/api/stats/record?period=x").status_code, 400)
        nick = client.get("/api/stats/record/nick?nick=peerbot")
        self.assertEqual(nick.get_json()["figures"]["files_received"], 1)
        export = client.get("/api/stats/record.csv?period=all")
        self.assertEqual(export.status_code, 200)
        self.assertIn('filename="dccore-transfers-all.csv"', export.headers["Content-Disposition"])
        self.assertEqual(export.get_data(as_text=True).count("\r\n"), 7)
        self.assertEqual(client.get("/api/stats/record.csv?period=x").status_code, 400)
        with contextlib.redirect_stdout(io.StringIO()):
            forgot = client.post("/api/stats/record/forget", json={"nick": "peerbot"})
        self.assertEqual(forgot.get_json()["removed"], 1)

    def test_logged_out_it_answers_nothing(self):
        client = webserver.create_app().test_client()
        for url in ("/api/stats/record", "/api/stats/record/nick?nick=listener", "/api/stats/record.csv"):
            with self.subTest(url=url):
                self.assertIn(client.get(url).status_code, (302, 401))
        self.assertIn(client.post("/api/stats/record/forget", json={"everyone": True}).status_code,
                      (302, 401, 403))
        self.assertEqual(webserver.build_record_payload("all")[1]["summary"]["files_sent"], 4)


def read(path):
    with io.open(os.path.join(REPO_ROOT, path), encoding="utf-8") as handle:
        return handle.read()


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function fn(signature) {
  const start = src.indexOf(signature);
  if (start < 0) { throw new Error("missing: " + signature); }
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") { depth++; }
    else if (src[i] === "}") { depth--; if (depth === 0) { break; } }
  }
  return src.slice(start, i + 1);
}

// Just enough of a DOM to see what is made, and to fail if anything is
// written as markup: innerHTML on any element throws.
function node(tag) {
  const n = {
    tagName: tag, children: [], className: "", title: "", hidden: false, colSpan: 1,
    _text: "",
    classList: { toggle: function () {} },
    appendChild: function (child) { this.children.push(child); return child; },
    get textContent() { return this._text + this.children.map(function (c) { return c.textContent; }).join(""); },
    set textContent(v) { this._text = String(v); this.children = []; },
    set innerHTML(v) { throw new Error("innerHTML used: " + v); },
    get innerHTML() { throw new Error("innerHTML read"); }
  };
  return n;
}
const document = { createElement: node };
const el = {};
["recordStatus", "recordBody", "recordSent", "recordPeriodBox", "recordCards", "recordTopSent",
 "recordTopReceived", "recordImported"].forEach(function (k) { el[k] = node("div"); });
const state = {};
const t = function (key) { return key; };
// The Most downloaded tables are drawn by renderTopDownloads(), which escapes
// through renderTopTable(); here it only records what it was asked to draw.
const drawn = [];
const renderTopDownloads = function (top) { drawn.push(top); };

const make = new Function("document", "el", "state", "t", "renderTopDownloads",
  fn("function recordNote(") + "\n" + fn("function recordCell(") + "\n" +
  fn("function recordTable(") + "\n" + fn("function recordIsOn(") + "\n" +
  fn("function renderRecord(") + "\nreturn renderRecord;");
const renderRecord = make(document, el, state, t, renderTopDownloads);

const evil = "<img src=x onerror=alert(1)>";
renderRecord({
  enabled: true, includes_imported: true,
  summary: { files_sent: 1234, lists_sent: 2, bytes_sent_text: "1.0GB", top_speed_text: "2.00MB/s",
             average_speed_text: "1.00MB/s", queue_wait_text: "30 Sec", files_received: 0,
             bytes_received_text: "0B" },
  top_files: [{ name: evil, count: 3 }],
  top_albums: [{ name: "An Album", count: 2 }], albums_enabled: true,
  top_sent: [{ nick: evil, files: 2, bytes_text: "8.0KB" }],
  top_received: []
});
const out = {
  cards: el.recordCards.children.length,
  firstCard: el.recordCards.children[0].textContent,
  drawn: drawn.slice(),
  sentShown: !el.recordSent.hidden,
  periodShown: !el.recordPeriodBox.hidden,
  cardLabels: el.recordCards.children.map(function (c) { return c.children[1].textContent; }),
  nickRow: el.recordTopSent.children[0].children.map(function (c) { return c.textContent; }),
  emptyReceived: el.recordTopReceived.children[0].className + "|" + el.recordTopReceived.children[0].textContent,
  importedShown: !el.recordImported.hidden,
  bodyShown: !el.recordBody.hidden,
  kept: state.lastRecord !== undefined
};
state.lastStats = { top: { files: [{ name: "all time", count: 9 }], albums: [] } };
renderRecord({ enabled: false });
out.offDrawn = drawn[drawn.length - 1];
out.offSentHidden = el.recordSent.hidden;
out.offPeriodHidden = el.recordPeriodBox.hidden;
out.offNote = el.recordStatus.textContent;
out.offBodyHidden = el.recordBody.hidden;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class ThePage(unittest.TestCase):
    def test_every_nick_and_name_is_text(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        got = json.loads(done.stdout.decode("utf-8"))
        evil = "<img src=x onerror=alert(1)>"
        self.assertEqual(got["cards"], 8)
        # toLocaleString(): the separator is the machine's ("1,234", "1.234", "1 234").
        self.assertRegex(got["firstCard"], r"^1\D?234stats\.recordFilesSent$")
        # One row, in the order agreed with the operator (#1117).
        self.assertEqual(got["cardLabels"], [
            "stats.recordFilesSent", "stats.recordBytesSent", "stats.recordListsSent",
            "stats.recordTopSpeed", "stats.recordAverageSpeed", "stats.recordQueueWait",
            "stats.recordFilesReceived", "stats.recordBytesReceived"])
        # The period's own most-sent files and albums go to the page's two
        # Most downloaded tables, which write names as text.
        self.assertEqual(got["drawn"], [{"files": [{"name": evil, "count": 3}],
                                         "albums": [{"name": "An Album", "count": 2}],
                                         "albums_enabled": True}])
        self.assertTrue(got["sentShown"] and got["periodShown"])
        self.assertEqual(got["nickRow"], [evil, "2", "8.0KB"])
        self.assertEqual(got["emptyReceived"], "empty-row|stats.recordNoNicks")
        self.assertTrue(got["importedShown"] and got["bodyShown"] and got["kept"])
        self.assertEqual((got["offNote"], got["offBodyHidden"]), ("stats.recordOff", True))
        # Record off: Sent and Period go, and Most downloaded falls back to
        # the all-time lists /api/stats carries.
        self.assertTrue(got["offSentHidden"] and got["offPeriodHidden"])
        self.assertEqual(got["offDrawn"], {"files": [{"name": "all time", "count": 9}], "albums": []})


REVIEW_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function fn(signature) {
  const start = src.indexOf(signature);
  if (start < 0) { throw new Error("missing: " + signature); }
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") { depth++; }
    else if (src[i] === "}") { depth--; if (depth === 0) { break; } }
  }
  return src.slice(start, i + 1);
}
const out = {};
const fillIn = new Function(fn("function fillIn(") + "\nreturn fillIn;")();
// "{sent}" and "{count}" are legal nicks.
out.line = fillIn("{nick}: {sent} file(s), {lists} list(s)", { nick: "{sent}", sent: "5", lists: "0" });
out.forgot = fillIn("{nick} is forgotten ({count} row(s) removed).", { nick: "{count}", count: "3" });
out.unknownKept = fillIn("{nick} {other}", { nick: "a" });

// Two periods asked one after the other, answered in the other order.
const record = { period: "all" };
const pending = [];
const drawn = [];
const loadRecord = new Function("record", "fetchJsonAllowingError", "renderRecord", "recordNote", "t",
  fn("function loadRecord(") + "\nreturn loadRecord;")(
  record,
  function () { return new Promise(function (resolve) { pending.push(resolve); }); },
  function (data) { drawn.push(data.period); },
  function () {}, function (key) { return key; });
const first = loadRecord();          // "all" - slow
record.period = "day";
const second = loadRecord();         // "day" - fast
pending[1]({ ok: true, status: 200, data: { period: "day" } });
pending[0]({ ok: true, status: 200, data: { period: "all" } });
Promise.all([first, second]).then(function () {
  out.drawn = drawn;
  console.log(JSON.stringify(out));
});
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class WhatTheReviewFound(unittest.TestCase):
    """#1102 review: a nick that is a placeholder, and answers out of order."""

    def test_nicks_are_never_read_as_placeholders_and_a_stale_answer_is_dropped(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(REVIEW_HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        got = json.loads(done.stdout.decode("utf-8"))
        self.assertEqual(got["line"], "{sent}: 5 file(s), 0 list(s)")
        self.assertEqual(got["forgot"], "{count} is forgotten (3 row(s) removed).")
        self.assertEqual(got["unknownKept"], "a {other}")
        self.assertEqual(got["drawn"], ["day"])

    def test_every_nick_message_goes_through_it(self):
        js = read("web/app.js")
        block = js.split("// ------------------------------------------------------ Transfer record", 1)[1]
        block = block.split("el.recordPeriods.addEventListener", 1)[0]
        self.assertNotIn('.replace("{nick}"', block)


class ThePageSource(unittest.TestCase):
    def test_it_is_loaded_on_opening_stats_and_never_on_the_poll(self):
        js = read("web/app.js")
        self.assertIn('if (name === "stats") { loadStats(); loadRecord(); }', js)
        poll = js.split("// Only while Live Transfers is the view on screen.", 1)[1].split("}, REFRESH_MS);", 1)[0]
        self.assertIn("loadLive();", poll)   # the poll is Live Transfers' own since #1123
        self.assertNotIn("loadRecord", poll)

    def test_a_language_switch_redraws_it(self):
        self.assertIn("if (state.lastRecord) { renderRecord(state.lastRecord); }", read("web/app.js"))

    def test_forgetting_is_asked_first(self):
        js = read("web/app.js")
        body = js.split("function forgetRecord(", 1)[1].split("\n  }\n", 1)[0]
        self.assertLess(body.index("window.confirm("), body.index('postJson("/api/stats/record/forget"'))

    def test_the_section_is_on_the_stats_page(self):
        html = read("web/index.html")
        stats = html.split('id="view-stats"', 1)[1].split("</section>", 1)[0]
        for marker in ('id="record-periods"', 'id="record-cards"', 'id="record-top-sent"',
                       'id="st-top-files"', 'id="st-top-albums"',
                       'id="record-forget-all"', 'href="/api/stats/record.csv?period=all"'):
            with self.subTest(marker=marker):
                self.assertIn(marker, stats)


if __name__ == "__main__":
    unittest.main()
