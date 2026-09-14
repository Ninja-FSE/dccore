"""Bringing an OmenServe operator's history across (#69).

The single biggest barrier to trying this bot is not features - it is
abandoning years of totals. `omenserve_import.py` already reads the numbers
out of mIRC's `scripts/vars.ini`; this is the half that puts them somewhere,
and the half that has to be careful.

THREE THINGS IT MUST NOT DO

Write a zero over a real total. A variable that is absent produces an absent
field, never a zero, all the way from the parser to the write - so an operator
running one add-on and not another imports what they have and keeps the rest.

Take a number on trust. A `vars.ini` is a plain text file people hand-edit,
and everything out of it is untrusted input. The operator confirming a figure
is not the same as the figure being sane, so the ceilings are enforced at the
endpoint and not only in the page.

Say "added" when it means "replaced". On a fresh install nobody reads that
sentence; on a used one it is the only thing that matters.

AND ONE THE PAGE OWNS

The real file on the install #69 was written from held 280 variables - nicks,
channels, paths and passwords among them. The page keeps only the counter
lines before sending, which is the only place that filtering can happen and
still be filtering. The list of what to keep is served rather than hard-coded,
so it cannot drift from the parser.
"""

import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import db  # noqa: E402
import defaults as config  # noqa: E402
import omenserve_import  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

# Real values from the live install #69 records, so the formats are
# established rather than guessed - including the nick riding along on the
# speed record and the variables that are none of our business.
REAL_VARS = """n1=%mx.rarsent 45902
n2=%mx.rartsent 16295360049140
n3=%SDmaxspeed 48762663,SomeNick
n4=%OSL.SentToday 6
n5=%OSL.SentYesterday 3
n6=%os.totalsize 1635488685616
n7=%sdlistsent 1308
n8=%mynick TheOperator
n9=%mypassword hunter2
n10=%OSL.Today Friday
"""


class ImportCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="dccore-stats-import-")
        # The two paths are reached differently, which is worth knowing when
        # redirecting them: db reads STATS_FILE off config on every call, and
        # holds SPEED_RECORD_FILE as a constant of its own.
        self.set_config(STATS_FILE=os.path.join(self.dir, "stats.txt"))
        self.real_speed = db.SPEED_RECORD_FILE
        db.SPEED_RECORD_FILE = os.path.join(self.dir, "speed.txt")
        self.addCleanup(setattr, db, "SPEED_RECORD_FILE", self.real_speed)

    def given_existing(self, files=0, total_bytes=0, record=0):
        # A REAL date in the seventh column. The row is written space-separated
        # and read back by splitting on whitespace, so an empty date produces
        # six columns and the loader - correctly - treats that as corrupt and
        # returns zeros. The first version of this helper did exactly that,
        # and every "an existing figure is preserved" test failed against
        # code that was preserving it perfectly well.
        db.save_advanced_stats([files, total_bytes, 0, 0, 0, 0, "2026-09-05"])
        db.save_speed_record(record)


class WhatAnInstallOffers(ImportCase):

    def test_the_three_figures_come_across(self):
        preview = webserver.build_stats_import_preview(REAL_VARS)

        self.assertEqual(preview["values"], {
            "total_files": 45902,
            "total_bytes": 16295360049140,
            "speed_record": 48762663,
        })

    def test_the_preview_changes_nothing(self):
        self.given_existing(files=7, total_bytes=99, record=5)

        webserver.build_stats_import_preview(REAL_VARS)

        self.assertEqual(webserver.current_importable_stats(),
                         {"total_files": 7, "total_bytes": 99,
                          "speed_record": 5})

    def test_it_shows_the_figures_it_will_not_import_too(self):
        """Library size and lists sent have nowhere to go here, and saying so
        is better than silently dropping rows the operator can see in their
        own file."""
        rows = {row["label"]: row for row
                in webserver.build_stats_import_preview(REAL_VARS)["rows"]}

        self.assertEqual(rows["Library size"]["value"], 1635488685616)
        self.assertFalse(rows["Library size"]["imported"])

    def test_it_reports_what_is_there_now_beside_it(self):
        self.given_existing(files=7, total_bytes=99, record=5)

        preview = webserver.build_stats_import_preview(REAL_VARS)

        self.assertEqual(preview["current"]["total_files"], 7)

    def test_it_names_what_would_be_replaced_rather_than_added_to(self):
        """The one line that matters on a used install, and the page shows the
        warning exactly when this is non-empty."""
        self.given_existing(files=7, total_bytes=99, record=5)

        preview = webserver.build_stats_import_preview(REAL_VARS)

        self.assertEqual(sorted(preview["replaces"]),
                         ["speed_record", "total_bytes", "total_files"])

    def test_a_fresh_install_is_not_warned_about_replacing_nothing(self):
        preview = webserver.build_stats_import_preview(REAL_VARS)

        self.assertEqual(preview["replaces"], {})

    def test_an_install_without_the_add_ons_says_so(self):
        preview = webserver.build_stats_import_preview(
            "n1=%mynick TheOperator\nn2=%OSL.Today Friday\n")

        self.assertEqual(preview["values"], {})
        self.assertTrue(any("add-ons" in note for note in preview["notes"]))


class NothingIsTakenOnTrust(ImportCase):
    """A vars.ini is a plain text file people hand-edit. Enforced at the
    endpoint and not only in the page: the page is convenience, this is the
    boundary."""

    def refuse(self, raw):
        status, result = webserver.apply_stats_import(raw)
        self.assertEqual(status, 400, f"{raw!r} was accepted")
        return result["error"]

    def test_a_negative_figure_is_refused(self):
        self.assertIn("negative", self.refuse({"total_files": -1}))

    def test_something_that_is_not_a_number_is_refused(self):
        self.assertIn("not a whole number",
                      self.refuse({"total_files": "several"}))

    def test_a_boolean_is_refused_rather_than_counted_as_one(self):
        """int(True) is 1, so a JSON `true` would import as a file count of
        one without this - a wrong answer that looks like a real one."""
        self.assertIn("expected a whole number",
                      self.refuse({"total_files": True}))

    def test_an_absurd_magnitude_is_refused(self):
        """A stray digit in a hand-edited file is far likelier than a genuine
        value up here, and importing one silently is worse than refusing it:
        the operator can retype a number, but cannot tell that a total is
        wrong once it looks plausible."""
        self.assertIn("beyond anything real",
                      self.refuse({"total_bytes": 1 << 60}))
        self.assertIn("beyond anything real",
                      self.refuse({"speed_record": 99 * 1000 ** 3}))

    def test_importing_nothing_is_refused_rather_than_written(self):
        self.assertIn("Nothing to import", self.refuse({}))

    def test_a_refused_import_writes_nothing(self):
        self.given_existing(files=7, total_bytes=99, record=5)

        webserver.apply_stats_import({"total_files": -1})

        self.assertEqual(webserver.current_importable_stats()["total_files"], 7)

    def test_one_bad_field_refuses_the_whole_import(self):
        """Not a partial write. Half an import leaves the operator with
        totals that came from two different places and no way to tell."""
        self.given_existing(files=7, total_bytes=99, record=5)

        webserver.apply_stats_import({"total_files": 45902,
                                      "speed_record": -1})

        self.assertEqual(webserver.current_importable_stats()["total_files"], 7)


class WritingIt(ImportCase):

    def test_the_figures_land(self):
        status, result = webserver.apply_stats_import({
            "total_files": 45902, "total_bytes": 16295360049140,
            "speed_record": 48762663})

        self.assertEqual(status, 200)
        self.assertEqual(webserver.current_importable_stats(), {
            "total_files": 45902, "total_bytes": 16295360049140,
            "speed_record": 48762663})

    def test_it_reports_the_before_and_after(self):
        """What actually happened, rather than what was asked for."""
        self.given_existing(files=7, total_bytes=99, record=5)

        _status, result = webserver.apply_stats_import({"total_files": 45902})

        self.assertEqual(result["before"]["total_files"], 7)
        self.assertEqual(result["after"]["total_files"], 45902)

    def test_an_absent_figure_is_left_alone_rather_than_zeroed(self):
        """The whole reason the parser reports absence rather than zero. An
        operator missing an add-on imports what they have and keeps the rest -
        writing a zero over a real total is the worst thing this could do."""
        self.given_existing(files=7, total_bytes=99, record=5)

        webserver.apply_stats_import({"total_files": 45902})

        now = webserver.current_importable_stats()
        self.assertEqual(now["total_files"], 45902)
        self.assertEqual(now["total_bytes"], 99)
        self.assertEqual(now["speed_record"], 5)

    def test_it_does_not_touch_what_the_bot_did_today(self):
        """The day columns and the date belong to the daemon's own rotation.
        Importing a lifetime total must not reset them - and the day markers
        in vars.ini are weekday NAMES, so they could not be imported anyway."""
        db.save_advanced_stats([1, 2, 30, 40, 50, 60, "2026-09-05"])

        webserver.apply_stats_import({"total_files": 45902})

        row = db.load_advanced_stats()
        self.assertEqual(list(row[2:6]), [30, 40, 50, 60])
        self.assertEqual(row[6], "2026-09-05")

    def test_a_string_of_digits_is_accepted(self):
        """Everything arriving from a text file is a string; refusing them
        would refuse the ordinary case."""
        status, _result = webserver.apply_stats_import({"total_files": "45902"})

        self.assertEqual(status, 200)
        self.assertEqual(webserver.current_importable_stats()["total_files"],
                         45902)


class WhatTheAuditFound(ImportCase):
    """Three from the audit of this branch, all in reading numbers out of a
    file somebody hand-edits."""

    def test_a_present_zero_does_not_wipe_a_real_total(self):
        """The worst thing this feature could do, and it was doing it.

        `number is not None` let a present zero through, so a fresh OmenServe
        install's `%mx.rartsent 0` silently replaced a real DCCore total - while
        the note beside it promised "the total size will stay as it is". A zero
        carries no history across, which is the entire point of the feature, so
        it can only destroy.
        """
        self.given_existing(files=45902, total_bytes=16295360049140, record=5)

        preview = webserver.build_stats_import_preview(
            "n1=%mx.rarsent 0\nn2=%mx.rartsent 0\n")

        self.assertEqual(preview["values"], {})
        webserver.apply_stats_import(preview["values"])
        self.assertEqual(webserver.current_importable_stats()["total_bytes"],
                         16295360049140)

    def test_a_present_zero_is_still_shown_and_explained(self):
        """The operator can see that number in their own file. A row that
        quietly vanished would read as a parse failure."""
        preview = webserver.build_stats_import_preview("n1=%mx.rarsent 0\n")

        row = [r for r in preview["rows"] if r["variable"] == "%mx.rarsent"][0]
        self.assertEqual(row["value"], 0)
        self.assertFalse(row["imported"])
        self.assertTrue(any("zero" in note for note in preview["notes"]),
                        preview["notes"])

    def test_a_value_that_floats_to_infinity_does_not_crash_the_preview(self):
        """"1e999" parses as a float perfectly well and becomes inf;
        int(round(inf)) raises OverflowError, not ValueError, so it escaped
        the except and reached the route as a 500 on a hand-edited file."""
        preview = webserver.build_stats_import_preview("n1=%mx.rarsent 1e999\n")

        self.assertEqual(preview["values"], {})
        self.assertTrue(any("not a number" in n for n in preview["notes"]),
                        preview["notes"])

    def test_a_thousands_separator_is_read_rather_than_truncated(self):
        """`split(",")[0]` is what %SDmaxspeed's "<bytes>,<nick>" needs, and
        the `.replace(",", "")` after it could never see a comma - dead. So a
        value written "45,902" became 45: silently, plausibly, and three
        orders of magnitude wrong."""
        import omenserve_import

        self.assertEqual(omenserve_import._as_int("45,902"), 45902)
        # And the speed record's own shape still works: a comma followed by a
        # nick is a separator between fields, not inside a number.
        self.assertEqual(omenserve_import._as_int("48762663,SomeNick"), 48762663)

    def test_a_write_that_did_not_land_is_reported_as_a_failure(self):
        """Both writers swallow their own errors and return None - correct for
        them, since a failed stats write must not take the daemon down - which
        meant a 200 and "imported" for figures that never reached the disk.
        The operator would be told their history came across and find half of
        it, with nothing to say which half."""
        import db

        real = db.save_speed_record
        db.save_speed_record = lambda *_a, **_k: None
        self.addCleanup(setattr, db, "save_speed_record", real)

        status, result = webserver.apply_stats_import(
            {"total_files": 45902, "speed_record": 48762663})

        self.assertEqual(status, 500)
        self.assertEqual(result["failed"], ["speed_record"])
        self.assertEqual(result["imported"], ["total_files"])

    def test_a_write_that_landed_is_still_reported_as_success(self):
        """Control: the failure path must not fire on an ordinary import."""
        status, result = webserver.apply_stats_import(
            {"total_files": 45902, "speed_record": 48762663})

        self.assertEqual(status, 200)
        self.assertEqual(result["imported"], ["speed_record", "total_files"])


class ThePageSendsOnlyTheCounters(unittest.TestCase):
    """The real file held 280 variables - nicks, channels, paths, passwords.

    None of that needs to cross the network for this to work, so the page
    keeps only the counter lines before sending. That filtering can only
    happen in the page: anywhere else and it is not filtering.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "web", "app.js"),
                  encoding="utf-8") as handle:
            cls.js = handle.read()

    def test_the_page_filters_before_it_posts(self):
        self.assertIn("function keepOnlyCounterLines(", self.js)
        block = self.js.split("function previewImportText(", 1)[1]
        block = block.split("\n  }", 1)[0]

        self.assertIn("keepOnlyCounterLines(", block,
                      "the whole file is posted, passwords and all")

    def test_the_list_of_what_to_keep_comes_from_the_server(self):
        """Hard-coding it in app.js is how it would drift from the parser: a
        field added to FIELDS would then be stripped out before arriving, and
        the operator told their file has nothing in it."""
        block = self.js.split("function importVariables(", 1)[1]
        block = block.split("\n  }", 1)[0]

        self.assertIn("/api/stats/import/variables", block)

    def test_the_route_answers_with_exactly_what_the_parser_knows(self):
        served = set(omenserve_import.variable_names())
        declared = {field.variable for field in omenserve_import.FIELDS}

        self.assertEqual(served, declared)


if __name__ == "__main__":
    unittest.main()
