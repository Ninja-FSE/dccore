"""KeepTrack's lifetime totals come in with the OmenServe import (#1062).

KeepTrack (by ^OmeN^, like OmenServe) keeps its totals in the same vars.ini:
files and bytes sent, files and bytes received, when it began counting, and
which file types it counted at all. Its SENT totals counted the same sends the
OmenServe add-ons did, so the two are never added: the operator picks one. Its
RECEIVED totals go into the transfer record (#1068) as imported figures, which
every all-time figure adds in and a period leaves out.
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

OMENSERVE = ["n1=%mx.rarsent 100", "n2=%sdmpxsent 50", "n3=%mx.rartsent 999"]
KEEPTRACK = ["n4=%KT.MPX.Sent 140", "n5=%KT.MPX.Sent.Total 5000", "n6=%KT.MPX.Gets 7",
             "n7=%KT.MPX.Gets.Total 700", "n8=%KT.Start.Date 3rd February 2002"]
DEFAULT_TYPES = "n9=%KT.Files " + omenserve_import.KT_DEFAULT_FILE_TYPES


def vars_ini(*lines):
    return "\n".join(lines)


class TheParse(unittest.TestCase):
    def test_both_sources_are_kept_apart_and_omenserve_stays_the_default(self):
        found = omenserve_import.read_install(vars_ini(*OMENSERVE, *KEEPTRACK))
        self.assertEqual((found["values"]["total_files"], found["values"]["total_bytes"]), (150, 999),
                         "the sent totals are one source's, never the two added")
        self.assertEqual(found["sent_source"], "omenserve")
        self.assertEqual([(s["name"], s["total_files"], s["total_bytes"]) for s in found["sent_sources"]],
                         [("omenserve", 150, 999), ("keeptrack", 140, 5000)])
        self.assertTrue(any("never added together" in n for n in found["notes"]))

    def test_keeptrack_alone_is_the_source(self):
        found = omenserve_import.read_install(vars_ini(*KEEPTRACK))
        self.assertEqual(found["sent_source"], "keeptrack")
        self.assertEqual((found["values"]["total_files"], found["values"]["total_bytes"]), (140, 5000))
        self.assertFalse(any("never added together" in n for n in found["notes"]))

    def test_received_and_its_start_date(self):
        found = omenserve_import.read_install(vars_ini(*KEEPTRACK))
        self.assertEqual((found["values"]["received_files"], found["values"]["received_bytes"],
                          found["values"]["received_since"]), (7, 700, "2002-02-03"))
        self.assertIn("KeepTrack has counted since 3 February 2002.", found["notes"])

    def test_a_start_date_it_cannot_read_is_a_note_not_an_error(self):
        found = omenserve_import.read_install(vars_ini(*KEEPTRACK[:4], "n8=%KT.Start.Date sometime"))
        self.assertNotIn("received_since", found["values"])
        self.assertEqual(found["values"]["received_files"], 7)
        self.assertTrue(any("could not be read" in n for n in found["notes"]))

    def test_the_rows_say_whose_figure_each_is(self):
        rows = {r["variable"]: r for r in omenserve_import.read_install(vars_ini(*KEEPTRACK))["rows"]}
        self.assertEqual((rows["%KT.MPX.Gets"]["source"], rows["%KT.MPX.Gets"]["target"]),
                         ("keeptrack", "received_files"))

    def test_start_dates(self):
        cases = {"1st January 2003": "2003-01-01", "2nd March 2004": "2004-03-02",
                 "3rd February 2002": "2002-02-03", "11th May 2010": "2010-05-11",
                 "21st March 2005": "2005-03-21", "31st February 2002": None, "junk": None, "": None}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(omenserve_import.kt_start_date(raw), expected)


class TheFileTypesNote(unittest.TestCase):
    def notes(self, *extra):
        return " ".join(omenserve_import.read_install(vars_ini(*KEEPTRACK, *extra))["notes"])

    def test_the_default_list_is_said_to_leave_out_rar_and_flac(self):
        self.assertIn("no .rar and no .flac", self.notes(DEFAULT_TYPES))

    def test_another_list_is_named_without_that(self):
        notes = self.notes("n9=%KT.Files *.mp3,*.flac")
        self.assertIn("only counted these file types: *.mp3,*.flac", notes)
        self.assertNotIn("no .rar and no .flac", notes)

    def test_all_types_needs_no_note(self):
        self.assertNotIn("only counted these file types", self.notes("n9=%KT.Files *"))


class Case(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="dccore-kt-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.set_config(TRANSFER_LOG_FILE=os.path.join(self.dir, "transfers.db"))
        support.no_disk_writes(__import__("db"))


class IntoTheRecord(Case):
    def test_received_totals_land_and_every_all_time_figure_adds_them(self):
        transfer_log.record_received("file", 300, nick="SomeBot")
        status, result = webserver.apply_stats_import(
            {"received_files": 7, "received_bytes": 700, "received_since": "2002-02-03"})
        self.assertEqual(status, 200, result)
        self.assertEqual(sorted(result["imported"]), ["received_bytes", "received_files", "received_since"])
        everything = transfer_log.summary()
        self.assertEqual((everything["files_received"], everything["bytes_received"]), (8, 1000))
        recent = transfer_log.summary(since=1)
        self.assertEqual((recent["files_received"], recent["bytes_received"]), (1, 300),
                         "a period is the record's own rows only")
        self.assertEqual(transfer_log.imported_totals("keeptrack")["received"],
                         {"files": 7, "bytes": 700, "since": "2002-02-03"})

    def test_importing_again_replaces_it(self):
        webserver.apply_stats_import({"received_files": 7, "received_bytes": 700})
        webserver.apply_stats_import({"received_files": 9, "received_bytes": 900})
        self.assertEqual(transfer_log.summary()["files_received"], 9)

    def test_the_before_and_after_show_it(self):
        webserver.apply_stats_import({"received_files": 7, "received_bytes": 700})
        current = webserver.current_importable_stats()
        self.assertEqual((current["received_files"], current["received_bytes"]), (7, 700))

    def test_a_date_that_is_not_one_is_refused(self):
        status, result = webserver.apply_stats_import({"received_files": 7, "received_since": "3rd Feb"})
        self.assertEqual(status, 400)
        self.assertIn("not a date", result["error"])

    def test_forgetting_everything_forgets_them_too(self):
        webserver.apply_stats_import({"received_files": 7, "received_bytes": 700})
        transfer_log.forget_all()
        self.assertEqual(transfer_log.summary()["files_received"], 0)
        self.assertEqual(transfer_log.imported_totals(), {})

    def test_with_the_record_off_the_preview_leaves_received_out_and_says_why(self):
        self.set_config(TRANSFER_LOG_FILE="")
        preview = webserver.build_stats_import_preview(vars_ini(*OMENSERVE, *KEEPTRACK))
        self.assertNotIn("received_files", preview["values"])
        self.assertEqual(preview["values"]["total_files"], 150, "the sent half still comes across")
        self.assertTrue(any("transfer record is off" in n for n in preview["notes"]))

    def test_a_date_whose_figure_was_refused_is_not_written_alone(self):
        """#1062 review: an out-of-range %KT.MPX.Gets was dropped and its start
        date kept; the apply then wrote the sent totals and answered 500 for a
        date nothing would write."""
        broken = ["n6=%KT.MPX.Gets 99999999999999999999", "n8=%KT.Start.Date 3rd February 2002"]
        preview = webserver.build_stats_import_preview(vars_ini(*OMENSERVE, *broken))
        self.assertNotIn("received_files", preview["values"])
        self.assertNotIn("received_since", preview["values"])
        self.assertTrue(any("beyond anything real" in n for n in preview["notes"]), preview["notes"])
        status, result = webserver.apply_stats_import(preview["values"])
        self.assertEqual(status, 200, result)
        self.assertEqual(result.get("failed", []), [])
        clean, _errors = webserver.validate_import_values({"received_since": "2002-02-03"})
        self.assertEqual(clean, {})

    def test_the_source_not_shown_first_is_checked_too(self):
        """#1062 review: only the default source was validated, so KeepTrack's
        -5 was offered as a choice and refused only when picked."""
        bad_keeptrack = ["n4=%KT.MPX.Sent 140", "n5=%KT.MPX.Sent.Total -5"]
        preview = webserver.build_stats_import_preview(vars_ini(*OMENSERVE, *bad_keeptrack))
        offered = {s["name"]: s for s in preview["sent_sources"]}
        self.assertEqual(offered["keeptrack"]["total_files"], 140)
        self.assertNotIn("total_bytes", offered["keeptrack"])
        self.assertTrue(any(n.startswith("KeepTrack") and "negative" in n for n in preview["notes"]),
                        preview["notes"])
        # Picked, what is left of it imports.
        values = {k: v for k, v in preview["values"].items() if k not in ("total_files", "total_bytes")}
        values.update({k: offered["keeptrack"][k] for k in ("total_files", "total_bytes") if k in offered["keeptrack"]})
        self.assertEqual(webserver.apply_stats_import(values)[0], 200)

    def test_the_preview_offers_the_sources(self):
        preview = webserver.build_stats_import_preview(vars_ini(*OMENSERVE, *KEEPTRACK))
        self.assertEqual([s["name"] for s in preview["sent_sources"]], ["omenserve", "keeptrack"])
        self.assertEqual(preview["sent_source"], "omenserve")


class TheFirstRunQuestion(Case):
    def test_it_asks_which_sent_totals_to_keep(self):
        import configure
        path = os.path.join(self.dir, "vars.ini")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(vars_ini(*OMENSERVE, *KEEPTRACK))
        answers = iter(["y", path, "2", "y"])
        written = []
        real = webserver.apply_stats_import
        webserver.apply_stats_import = lambda values: (written.append(values) or (200, {"imported": []}))
        self.addCleanup(setattr, webserver, "apply_stats_import", real)
        said = []
        self.assertTrue(configure.offer_to_import_omenserve_stats(ask=lambda _q: next(answers), log=said.append))
        self.assertEqual((written[0]["total_files"], written[0]["total_bytes"]), (140, 5000))
        self.assertEqual(written[0]["received_files"], 7)
        self.assertIn("  Keeping KeepTrack's sent totals.", said)


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
const rendered = [];
const payload = {
  values: { total_files: 150, total_bytes: 999, received_files: 7 },
  sent_source: "omenserve",
  sent_sources: [{ name: "omenserve", label: "OmenServe add-ons", total_files: 150, total_bytes: 999 },
                 { name: "keeptrack", label: "KeepTrack", total_files: 140 }]
};
const state = { importPayload: payload };
const choose = new Function("state", "renderImportPreview",
  fn("function chooseImportSource(") + "\nreturn chooseImportSource;")(
  state, function (p) { rendered.push(JSON.parse(JSON.stringify(p))); });
choose("keeptrack");
choose("nonsense");
console.log(JSON.stringify(rendered));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class ThePageSwitchesTheSource(unittest.TestCase):
    def test_picking_keeptrack_posts_only_its_sent_totals_and_keeps_the_rest(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        rendered = json.loads(done.stdout.decode("utf-8"))
        self.assertEqual(len(rendered), 1, "an unknown source changes nothing")
        # KeepTrack gave a file count and no bytes here: OmenServe's bytes must
        # not stay behind beside it - the two sources are never mixed.
        self.assertEqual(rendered[0]["values"], {"total_files": 140, "received_files": 7})
        self.assertEqual(rendered[0]["sent_source"], "keeptrack")


if __name__ == "__main__":
    unittest.main()
