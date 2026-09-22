"""_flush_known_bots recorded a flush even when save_known_bots swallowed
the error (audit L27, #691).

db.save_known_bots() catches every exception and returned None;
_flush_known_bots() then set runtime.known_bots_flushed_at regardless and
returned True. A failed write - disk full, a permission, a replace that
outlasted its retries - was therefore not tried again for
KNOWN_BOTS_FLUSH_SECONDS (30 s); the dashboard's add-source and
remove-source routes answered a plain 200 for a row that was not on disk;
and shutdown did no final flush, so a Ctrl-C in the window lost it.

save_known_bots() answers True or False and serialises a snapshot (the IRC
thread inserts a bot in place while a dashboard request flushes);
_flush_known_bots() stamps the flush time only on True; the two dashboard
routes carry a warning when the write did not land; and the Ctrl-C path
flushes once more on the way out.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

T0 = 1_700_000_000.0


class _Quiet(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        runtime.known_bots.clear()
        self.addCleanup(runtime.known_bots.clear)
        runtime.known_bots_flushed_at = 0.0
        self.addCleanup(setattr, runtime, "known_bots_flushed_at", 0.0)
        self.dir = tempfile.mkdtemp(prefix="dccore-registry-")
        self._real_file = db.KNOWN_BOTS_FILE
        db.KNOWN_BOTS_FILE = os.path.join(self.dir, "known_bots.json")
        self.addCleanup(setattr, db, "KNOWN_BOTS_FILE", self._real_file)
        self.out = io.StringIO()
        self._quiet = contextlib.redirect_stdout(self.out)
        self._quiet.__enter__()
        self.addCleanup(self._quiet.__exit__, None, None, None)

    def disk_refuses(self):
        real = db._atomic_write

        def refuse(*_a, **_k):
            raise OSError("disk full")
        db._atomic_write = refuse
        self.addCleanup(setattr, db, "_atomic_write", real)


class TheFlush(_Quiet):

    def test_a_save_that_landed_is_true_and_one_that_did_not_is_false(self):
        runtime.known_bots["a"] = {"nick": "a"}
        self.assertTrue(db.save_known_bots(runtime.known_bots))
        self.disk_refuses()

        self.assertFalse(db.save_known_bots(runtime.known_bots))
        self.assertIn("[DB ERROR] Could not save the bot registry", self.out.getvalue())

    def test_a_failed_flush_is_not_recorded_as_one(self):
        """The audit's probe: the disk refuses, and the stamp stays where
        it was, so the next advert tries again."""
        self.disk_refuses()
        runtime.known_bots["a"] = {"nick": "a"}

        self.assertFalse(irc._flush_known_bots(now=T0, force=True))

        self.assertEqual(runtime.known_bots_flushed_at, 0.0)
        # and, unforced, the very next call still tries rather than waiting out the interval
        self.assertFalse(irc._flush_known_bots(now=T0 + 1))
        self.assertIn("Could not save the bot registry", self.out.getvalue())

    def test_a_flush_that_landed_is_recorded(self):
        runtime.known_bots["a"] = {"nick": "a"}

        self.assertTrue(irc._flush_known_bots(now=T0, force=True))

        self.assertEqual(runtime.known_bots_flushed_at, T0)
        with io.open(db.KNOWN_BOTS_FILE, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"a": {"nick": "a"}})

    def test_the_live_dict_may_change_while_it_is_written(self):
        """A snapshot is serialised: a registry whose entries grow under the
        writer (the IRC thread's in-place update) is not a RuntimeError."""
        class Grows(dict):
            def items(self):
                # json.dumps of a plain dict that gains a key mid-way raises;
                # the snapshot is taken before any of that.
                runtime.known_bots["late"] = {"nick": "late"}
                return super().items()
        registry = Grows({"a": {"nick": "a"}})

        self.assertTrue(db.save_known_bots(registry))


class TheDashboardRoutesSaySo(_Quiet):

    def test_add_source_warns_when_the_registry_did_not_reach_the_disk(self):
        self.disk_refuses()

        status, result = webserver.build_add_source_result("SomeBot")

        self.assertEqual(status, 200)
        self.assertEqual(result["added"], "SomeBot")
        self.assertIn("could not be written to disk", result["warning"])
        self.assertIn("somebot", runtime.known_bots, "the row is still kept in memory")

    def test_remove_source_warns_too(self):
        runtime.known_bots["somebot"] = {"nick": "SomeBot", "hand_entered": True}
        self.disk_refuses()

        status, result = webserver.build_remove_source_result("SomeBot")

        self.assertEqual(status, 200)
        self.assertIn("could not be written to disk", result["warning"])

    def test_and_neither_says_anything_when_it_landed(self):
        _s, added = webserver.build_add_source_result("SomeBot")
        _s, removed = webserver.build_remove_source_result("SomeBot")

        self.assertNotIn("warning", added)
        self.assertNotIn("warning", removed)


class ThePageShowsTheWarning(unittest.TestCase):

    def test_both_handlers_read_the_warning_and_every_language_has_the_text(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            app = handle.read()

        self.assertEqual(app.count('if (res.data.warning) {'), 2)
        self.assertEqual(app.count('t("filelists.sourceNotOnDisk")'), 2)
        for lang in ("en", "es", "fr"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"), encoding="utf-8") as handle:
                self.assertIn("filelists.sourceNotOnDisk", json.load(handle), lang)


class ShutdownFlushesOnce(unittest.TestCase):

    def test_the_ctrl_c_path_flushes_the_registry_before_exiting(self):
        with io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            body = handle.read()
        interrupt = body.index("except KeyboardInterrupt:")
        block = body[interrupt:body.index("sys.exit(0)", interrupt)]

        self.assertIn("_flush_known_bots(force=True)", block)


if __name__ == "__main__":
    unittest.main()
