"""How long the list rebuild has been going, and how long it took.

A rebuild of a large library on a mapped drive runs for minutes and sometimes
hours. Until now the dashboard said what it was DOING - "Scanning folder 3 of
10 · 4,211 files so far" - and never how long it had been doing it, so the one
question an operator actually has while watching it ("is this normal, or has
it hung?") had no answer on the page.

Three pieces, and they fail separately:

  * update_list.py stamps when the run began, in every progress write. It is a
    SUBPROCESS, so the file is the only thing it shares with the daemon.
  * webserver.read_list_progress() turns that into elapsed seconds, using the
    daemon's own clock at both ends.
  * commands.async_list_updater() times the whole operation - not just the
    scan - and records it for the page and the debug channel.
"""

import io
import json
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class SecondsAPersonCanRead(unittest.TestCase):
    """commands.describe_duration(). "4331s" is a number nobody converts in
    their head, and a rebuild really does run that long."""

    def test_under_a_minute_is_seconds(self):
        self.assertEqual(commands.describe_duration(7), "7s")
        self.assertEqual(commands.describe_duration(59), "59s")

    def test_a_minute_is_minutes_and_seconds(self):
        self.assertEqual(commands.describe_duration(60), "1m 00s")
        self.assertEqual(commands.describe_duration(64), "1m 04s")

    def test_the_smaller_unit_is_padded(self):
        """So consecutive rebuilds line up when they are read one under the
        other in a log."""
        self.assertEqual(commands.describe_duration(125), "2m 05s")

    def test_an_hour_drops_the_seconds(self):
        """At that length the seconds are noise, and the operator is asking
        "how many hours", not "how many seconds"."""
        self.assertEqual(commands.describe_duration(3600), "1h 00m")
        self.assertEqual(commands.describe_duration(4331), "1h 12m")

    def test_the_boundaries(self):
        """One second either side of each change of unit, because an
        off-by-one here shows "60m 00s" or "0h 59m" to a real operator."""
        self.assertEqual(commands.describe_duration(3599), "59m 59s")
        self.assertEqual(commands.describe_duration(0), "0s")

    def test_rubbish_does_not_raise(self):
        """It is a status line. Nothing here is worth an exception inside the
        thread that is rebuilding the list."""
        for value in (None, "soon", object()):
            with self.subTest(value=value):
                self.assertEqual(commands.describe_duration(value),
                                 "an unknown time")

    def test_a_negative_is_not_shown_as_one(self):
        """A clock that steps backwards mid-rebuild - ntp correcting a drift -
        must not produce "-4s"."""
        self.assertEqual(commands.describe_duration(-5), "0s")


class TheRunStampsWhenItBegan(unittest.TestCase):
    """update_list.py's side. Read as source rather than run, because running
    it means walking a library; what matters is that the field is in the
    payload every write shares."""

    @staticmethod
    def source():
        with io.open(os.path.join(REPO_ROOT, "update_list.py"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_every_progress_write_carries_it(self):
        """Not only the first. The dashboard may open at any point during a
        rebuild and reads whatever write landed last."""
        body = self.source().split("def write_progress(", 1)[1]
        payload = body.split("payload = {", 1)[1].split("}", 1)[0]

        self.assertIn("started_at", payload)

    def test_it_is_stamped_once_for_the_process(self):
        """Module scope, not per call. A rebuild is one process, so "when did
        this process start" is the honest answer to "how long has this been
        running" - and taken per write it would always read as zero."""
        body = self.source().split("def write_progress(", 1)[0]

        self.assertIn("_started_at = time.time()", body)


class HowLongItHasBeenGoing(DCCoreTestCase):
    """webserver.read_list_progress()."""

    def setUp(self):
        super().setUp()
        self.progress = os.path.join(self.make_tree().root, "progress.json")
        self.set_config(LIST_PROGRESS_FILE=self.progress)

    def write(self, payload):
        with io.open(self.progress, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_it_is_seconds_since_the_run_began(self):
        self.write({"phase": "scanning", "started_at": time.time() - 75})

        elapsed = webserver.read_list_progress()["elapsed"]

        self.assertGreaterEqual(elapsed, 75)
        self.assertLess(elapsed, 80, "elapsed drifted well past the real gap")

    def test_a_file_with_no_start_time_reports_none_not_zero(self):
        """A progress file written by an older build, read mid-upgrade.
        "Not reported" and "started at the epoch" are different claims, and
        the second renders as fifty-odd years."""
        self.write({"phase": "scanning", "folder_count": 2})

        self.assertIsNone(webserver.read_list_progress()["elapsed"])

    def test_a_start_time_that_is_not_a_number_reports_none(self):
        self.write({"phase": "scanning", "started_at": "yesterday"})

        self.assertIsNone(webserver.read_list_progress()["elapsed"])

    def test_a_clock_that_stepped_backwards_reports_zero_not_negative(self):
        """The child stamps its own clock. A machine whose time is corrected
        mid-rebuild would otherwise report a run that has not begun."""
        self.write({"phase": "scanning", "started_at": time.time() + 500})

        self.assertEqual(webserver.read_list_progress()["elapsed"], 0)

    def test_the_rest_of_the_progress_still_reads(self):
        """The control: elapsed is added beside what was already there, not
        instead of it."""
        self.write({"phase": "scanning", "folder": "Flac", "folder_index": 3,
                    "folder_count": 10, "files": 4211,
                    "started_at": time.time() - 5})

        progress = webserver.read_list_progress()

        self.assertEqual(progress["folder"], "Flac")
        self.assertEqual(progress["files"], 4211)
        self.assertEqual(progress["percent"], 20)


class HowLongItTook(DCCoreTestCase):
    """The finished total, which is a different measurement from the one
    above: it covers spawning python, the NFS sync pause after the child
    exits, and the re-count afterwards. On a slow mount those are not
    rounding."""

    def test_the_status_payload_carries_it(self):
        self.set_config(last_list_update_seconds=184)

        self.assertEqual(
            webserver.build_update_list_status_payload()["seconds"], 184)

    def test_it_is_none_until_a_rebuild_has_finished(self):
        """The same rule `ok` follows: never having run is not a claim about
        how long the last run took."""
        self.assertIsNone(
            webserver.build_update_list_status_payload()["seconds"])

    def test_every_ending_records_one(self):
        """Success, failure, timeout and the unexpected. A duration left over
        from the previous rebuild is worse than none - it would be shown
        against a run it did not measure."""
        with io.open(os.path.join(REPO_ROOT, "commands.py"),
                     encoding="utf-8") as handle:
            body = handle.read().split("def async_list_updater(", 1)[1]

        ok_lines = body.count("config.last_list_update_ok")
        timed = body.count("config.last_list_update_seconds")

        self.assertTrue(ok_lines)
        self.assertEqual(timed, ok_lines,
                         "a path that records an outcome without recording "
                         "how long it took leaves the previous rebuild's "
                         "duration showing")

    def test_the_clock_starts_inside_the_thread(self):
        """Not when the request arrives. !update can wait on the maintenance
        lock, and time spent queued is not time spent rebuilding."""
        with io.open(os.path.join(REPO_ROOT, "commands.py"),
                     encoding="utf-8") as handle:
            body = handle.read().split("def async_list_updater(", 1)[1]

        self.assertIn("started = time.time()", body.split("try:", 1)[0])

    def test_the_harness_resets_it(self):
        """A leftover across tests is the same defect as a leftover across
        rebuilds, and harder to see."""
        from tests import support

        self.assertIn("last_list_update_seconds", support.RUNTIME_FLAGS)


class TheChannelIsToldToo(unittest.TestCase):
    """The dashboard is not the only place a rebuild is watched - most
    operators read the debug channel."""

    @staticmethod
    def success_message():
        with io.open(os.path.join(REPO_ROOT, "commands.py"),
                     encoding="utf-8") as handle:
            body = handle.read()
        return body.split("List update successfully completed", 1)[1].split(
            "category=", 1)[0]

    def test_the_completion_message_says_how_long(self):
        self.assertIn("describe_duration(", self.success_message())

    def test_it_reports_the_same_measurement_the_page_does(self):
        """One number, one wording. Two would read as two different
        measurements of the same rebuild."""
        self.assertIn("started", self.success_message())


class TheTwoWordingsAgree(unittest.TestCase):
    """describe_duration() exists in Python and in JavaScript, because the
    same rebuild is reported in the debug channel and on the page. Nothing
    executes the JavaScript here, so this pins the shape rather than the
    output - the boundaries are what drifted when only one was edited."""

    @staticmethod
    def script():
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read().split("function describeDuration(", 1)[1] \
                                .split("\n  function ", 1)[0]

    def test_the_page_has_one_too(self):
        self.assertIn("return total +", self.script())

    def test_it_changes_unit_at_the_same_two_places(self):
        body = self.script()

        self.assertIn("total < 60", body)
        self.assertIn("total < 3600", body)

    def test_it_pads_the_smaller_unit_as_well(self):
        self.assertIn("padStart(2", self.script())

    def test_it_clamps_at_zero_as_well(self):
        self.assertIn("Math.max(0", self.script())


if __name__ == "__main__":
    unittest.main()
