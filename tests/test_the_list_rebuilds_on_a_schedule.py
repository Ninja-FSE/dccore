"""The list rebuilds itself on a schedule (#776).

Nothing rebuilt the list on a timer: !update, the dashboard's Update list
and the console's `update` each ran it once, and "on a schedule" in FUTURE.md
meant the operator's own cron - which a novice never sets up and which
bypassed PAUSE_ON_UPDATE and the in-progress guard. LIST_REBUILD_SCHEDULE
runs what !update runs, from a worker checking once a minute:

    daily 04:00 | weekly sun 04:00 | monthly 1 03:30 | every 12h | (empty: off)

The rules, each tested below: a missed slot runs once when the bot comes
back; a restart after today's rebuild does not rebuild again; "every Nh"
counts from the last rebuild of any kind; a rebuild that FAILS is tried
again at the next slot, not every minute; a rebuild already running is not
doubled. Local time, the bot's clock - the tests build their moments with
naive local datetimes, as the code reads them.
"""

import os
import sys
import unittest
from datetime import datetime
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import runtime  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_webserver import write_master_list  # noqa: E402


def at(*parts):
    """A local moment as a timestamp: at(2026, 6, 10, 4, 0)."""
    return datetime(*parts).timestamp()


class TheSchedule(unittest.TestCase):

    def test_the_four_forms(self):
        self.assertEqual(settings_file.parse_rebuild_schedule("daily 04:00"), ("daily", 4, 0))
        self.assertEqual(settings_file.parse_rebuild_schedule("weekly sun 04:00"), ("weekly", 6, 4, 0))
        self.assertEqual(settings_file.parse_rebuild_schedule("monthly 1 03:30"), ("monthly", 1, 3, 30))
        self.assertEqual(settings_file.parse_rebuild_schedule("every 12h"), ("every", 12))

    def test_the_forms_people_will_actually_type(self):
        self.assertEqual(settings_file.parse_rebuild_schedule("  Weekly Sunday 4:05 "), ("weekly", 6, 4, 5))
        self.assertEqual(settings_file.parse_rebuild_schedule("every 12 hours"), ("every", 12))
        self.assertEqual(settings_file.parse_rebuild_schedule("every 1 hour"), ("every", 1))

    def test_empty_is_off(self):
        self.assertIsNone(settings_file.parse_rebuild_schedule(""))
        self.assertIsNone(settings_file.parse_rebuild_schedule("   "))

    def test_anything_else_is_refused_and_the_four_forms_are_named(self):
        for text in ("daily 25:00", "daily 4", "weekly funday 04:00", "monthly 32 03:30",
                     "hourly", "every 0h", "every twelve hours", "tomorrow"):
            with self.subTest(text=text):
                problem = settings_file.rebuild_schedule_problem(text)
                self.assertIsNotNone(problem)
                for form in settings_file.REBUILD_SCHEDULE_FORMS:
                    self.assertIn(form, problem)

    def test_a_bad_value_is_refused_where_every_setting_is_checked(self):
        """coerce() is the one door every value comes through - settings.conf
        at startup, the Settings page, apply_settings_changes()."""
        with self.assertRaises(ValueError):
            settings_file.coerce("LIST_REBUILD_SCHEDULE", "sometimes", "")
        self.assertEqual(settings_file.coerce("LIST_REBUILD_SCHEDULE", " daily 04:00 ", ""), "daily 04:00")
        self.assertEqual(settings_file.coerce("LIST_REBUILD_SCHEDULE", "", ""), "")


class WhenItIsDue(unittest.TestCase):

    DAILY = ("daily", 4, 0)

    def test_a_bot_that_was_down_at_four_rebuilds_when_it_comes_back(self):
        self.assertTrue(commands.rebuild_is_due(self.DAILY, at(2026, 6, 10, 4, 5), at(2026, 6, 9, 4, 1)))

    def test_a_restart_after_todays_rebuild_does_not_rebuild_again(self):
        self.assertFalse(commands.rebuild_is_due(self.DAILY, at(2026, 6, 10, 23, 0), at(2026, 6, 10, 4, 1)))

    def test_before_the_slot_it_is_not_due(self):
        self.assertFalse(commands.rebuild_is_due(self.DAILY, at(2026, 6, 10, 3, 59), at(2026, 6, 9, 4, 1)))

    def test_a_list_never_built_is_due_at_once(self):
        self.assertTrue(commands.rebuild_is_due(self.DAILY, at(2026, 6, 10, 12, 0), None))

    def test_weekly_on_its_day(self):
        sunday = ("weekly", 6, 4, 0)
        # 2026-06-14 is a Sunday.
        self.assertTrue(commands.rebuild_is_due(sunday, at(2026, 6, 14, 4, 1), at(2026, 6, 10, 4, 0)))
        self.assertFalse(commands.rebuild_is_due(sunday, at(2026, 6, 13, 23, 0), at(2026, 6, 10, 4, 0)))

    def test_monthly_on_a_day_the_month_does_not_have_is_its_last_day(self):
        the_31st = ("monthly", 31, 3, 30)
        # June has 30 days: the slot is 30 June 03:30.
        self.assertTrue(commands.rebuild_is_due(the_31st, at(2026, 6, 30, 3, 31), at(2026, 5, 31, 3, 31)))
        self.assertFalse(commands.rebuild_is_due(the_31st, at(2026, 6, 30, 3, 29), at(2026, 5, 31, 3, 31)))

    def test_every_n_hours_counts_from_the_last_rebuild_of_any_kind(self):
        twelve = ("every", 12)
        self.assertFalse(commands.rebuild_is_due(twelve, at(2026, 6, 10, 20, 0), at(2026, 6, 10, 9, 0)))
        self.assertTrue(commands.rebuild_is_due(twelve, at(2026, 6, 10, 21, 0), at(2026, 6, 10, 9, 0)))

    def test_the_next_time_is_the_next_slot(self):
        after = commands._rebuild_slot_after(self.DAILY, datetime(2026, 6, 10, 4, 0))
        self.assertEqual(after, datetime(2026, 6, 11, 4, 0), "a slot AT now is not the next one")
        self.assertEqual(commands._rebuild_slot_after(("monthly", 31, 3, 30), datetime(2026, 6, 1)),
                         datetime(2026, 6, 30, 3, 30))


class TheTick(DCCoreTestCase):
    """scheduled_rebuild_tick() against a real published list file whose age
    is set, with the rebuild itself recorded instead of run."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.list_path = write_master_list(self.tree.lists, "SomeBot", [(None, [("A.flac", "1MB")])])
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, LIST_BASE_NAME="SomeBot",
                        LIST_REBUILD_SCHEDULE="daily 04:00", update_inprogress=False)
        self.addCleanup(setattr, runtime, "rebuild_schedule_last_attempt", None)
        runtime.rebuild_schedule_last_attempt = None
        self.started = []
        self.said = []
        for target, value in (
                ((commands, "handle_list_update_request"),
                 lambda user, chan, authorised=False, user_host=None: self.started.append((user, chan, authorised))),
                ((announce, "send_debug"), lambda text, category="INFO", **k: self.said.append(text))):
            patch = mock.patch.object(*target, value)
            patch.start()
            self.addCleanup(patch.stop)

    def list_built_at(self, moment):
        os.utime(self.list_path, (moment, moment))

    def test_when_due_it_runs_what_update_runs_and_says_so(self):
        self.list_built_at(at(2026, 6, 9, 4, 1))

        self.assertTrue(commands.scheduled_rebuild_tick(now=at(2026, 6, 10, 4, 1)))

        self.assertEqual(self.started, [("the rebuild schedule", "daily 04:00", True)])
        self.assertTrue(any("Scheduled list rebuild starting" in line for line in self.said), self.said)

    def test_when_not_due_it_does_nothing(self):
        self.list_built_at(at(2026, 6, 10, 4, 1))

        self.assertFalse(commands.scheduled_rebuild_tick(now=at(2026, 6, 10, 12, 0)))
        self.assertEqual(self.started, [])

    def test_a_rebuild_already_running_is_not_doubled(self):
        self.list_built_at(at(2026, 6, 9, 4, 1))
        self.set_config(update_inprogress=True)

        self.assertFalse(commands.scheduled_rebuild_tick(now=at(2026, 6, 10, 4, 1)))
        self.assertEqual(self.started, [])

    def test_a_failed_rebuild_is_not_started_again_every_minute(self):
        """The list file never got newer - the rebuild failed - so without the
        recorded attempt every following minute would start another."""
        self.list_built_at(at(2026, 6, 9, 4, 1))
        commands.scheduled_rebuild_tick(now=at(2026, 6, 10, 4, 1))

        for minute in range(2, 30):
            commands.scheduled_rebuild_tick(now=at(2026, 6, 10, 4, minute))

        self.assertEqual(len(self.started), 1)

    def test_and_is_tried_again_at_the_next_slot(self):
        self.list_built_at(at(2026, 6, 9, 4, 1))
        commands.scheduled_rebuild_tick(now=at(2026, 6, 10, 4, 1))

        self.assertTrue(commands.scheduled_rebuild_tick(now=at(2026, 6, 11, 4, 1)))
        self.assertEqual(len(self.started), 2)

    def test_off_does_nothing(self):
        self.set_config(LIST_REBUILD_SCHEDULE="")
        self.list_built_at(at(2020, 1, 1, 0, 0))

        self.assertFalse(commands.scheduled_rebuild_tick(now=at(2026, 6, 10, 4, 1)))
        self.assertEqual(self.started, [])


class TheWorker(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, runtime, "rebuild_schedule_started", runtime.rebuild_schedule_started)
        runtime.rebuild_schedule_started = False

    def test_it_is_started_once_and_only_with_a_schedule(self):
        starts = []
        self.set_config(LIST_REBUILD_SCHEDULE="")
        self.assertFalse(commands.ensure_rebuild_schedule_worker(start=lambda: starts.append(1)))

        self.set_config(LIST_REBUILD_SCHEDULE="every 12h")
        self.assertTrue(commands.ensure_rebuild_schedule_worker(start=lambda: starts.append(1)))
        self.assertFalse(commands.ensure_rebuild_schedule_worker(start=lambda: starts.append(1)),
                         "a second call - every rehash makes one - started a second worker")
        self.assertEqual(starts, [1])

    def test_it_waits_before_its_first_check(self):
        """So a bot just started connects before a due rebuild pauses it."""
        order = []

        class Stop(Exception):
            pass

        def sleep(seconds):
            order.append(("sleep", seconds))
            if len(order) > 2:
                raise Stop

        with mock.patch.object(commands, "scheduled_rebuild_tick", lambda: order.append(("tick",))), \
                mock.patch("builtins.print"):
            with self.assertRaises(Stop):
                commands.rebuild_schedule_worker(sleep=sleep)

        self.assertEqual(order[:3], [("sleep", 60.0), ("tick",), ("sleep", 60.0)])

    def test_boot_and_every_rehash_start_it(self):
        """Read from the source - neither can be run whole in a test (the
        same reason the automatic refresh's own wiring is read)."""
        import io
        with io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            self.assertIn("commands.ensure_rebuild_schedule_worker()", handle.read())
        with io.open(os.path.join(REPO_ROOT, "commands.py"), encoding="utf-8") as handle:
            rehash = handle.read().split("def _handle_rehash_request(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if ensure_rebuild_schedule_worker():", rehash)


class WhereTheOperatorSeesIt(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, runtime, "rebuild_schedule_last_attempt", None)

    def test_the_console_status_line(self):
        self.set_config(LIST_REBUILD_SCHEDULE="")
        self.assertEqual(commands.describe_rebuild_schedule(), "off")

        self.set_config(LIST_REBUILD_SCHEDULE="daily 04:00")
        with mock.patch.object(commands, "last_list_rebuild", lambda: at(2026, 6, 10, 4, 1)):
            line = commands.describe_rebuild_schedule(now=at(2026, 6, 10, 12, 0))
        self.assertTrue(line.startswith("daily 04:00 - next "), line)
        self.assertIn("04:00", line)

        with mock.patch.object(commands, "last_list_rebuild", lambda: at(2026, 6, 9, 4, 1)):
            self.assertEqual(commands.describe_rebuild_schedule(now=at(2026, 6, 10, 12, 0)),
                             "daily 04:00 - due now")

    def test_the_tools_page_is_told_the_schedule_and_the_next_time(self):
        self.set_config(LIST_REBUILD_SCHEDULE="every 12h")
        with mock.patch.object(commands, "next_scheduled_rebuild", lambda: 1234.0):
            payload = webserver.build_update_list_status_payload()

        self.assertEqual(payload["schedule"], "every 12h")
        self.assertEqual(payload["next_scheduled"], 1234.0)

    def test_the_tools_view_asks_for_it_when_it_opens(self):
        import io
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            app = handle.read()
        self.assertIn('if (name === "tools") { loadUpdateListSchedule(); }', app)
        self.assertIn('id="update-list-schedule"', io.open(
            os.path.join(REPO_ROOT, "web", "index.html"), encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
