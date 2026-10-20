"""#1462: the dashboard's Settings save learns whether its rehash worked.

#1434 made a save's rehash say whether its reload went through, and the
console and dccore.mrc pass that on (SETAPPLYERR). The dashboard's own save
passed no `on_applied`: its answer said "Rehash started", the page re-fetched
/api/settings, and an operator whose admin_config.py held a syntax error was
never told the saved values were not what the bot ran.

Now every save that reaches its rehash is numbered (runtime.save_rehash), the
rehash's thread records the outcome under that number, and the Settings page
polls GET /api/settings/rehash until it is known, then says so in the same
sentence as the console.

The rehash itself is stood in for here, as in
tests/test_a_failed_rehash_is_not_said_to_be_in_effect.py: what is under test
is what the save does with the answer, and a real reload mid-suite would reset
state the rest of the run stands on. The page's own functions run under node,
with a fake clock, timers and server.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests import support  # noqa: F401  (path setup)

import commands  # noqa: E402
import runtime  # noqa: E402
import web_settings  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
# Base classes only (no tests of their own), so the loader runs nothing twice.
from tests.test_a_failed_rehash_is_not_said_to_be_in_effect import ConsoleCase  # noqa: E402
from tests.test_console_settings_commands import make_session, run  # noqa: E402
from tests.test_every_route_is_behind_the_login import PASSWORD, RouteCase  # noqa: E402

REPO_ROOT = support.REPO_ROOT
HAVE_FLASK = webserver.HAVE_FLASK
NO_FLASK = "Flask not installed; CI installs requirements-web.txt"

# A rehash that raised: an admin_config.py with a syntax error gets caught by
# the real rehash itself, but one that escapes it must count as a failure too.
CRASH = RuntimeError("example rehash crash")


class RehashStandIn:
    """commands.handle_rehash_request replaced by one that answers as told,
    after `release` when held, recording the threads it ran on so a test can
    join them - and so know the save's `finally` has run - before it looks."""

    def __init__(self, case):
        self.case = case
        self.threads = []
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.raised = []
        case.addCleanup(setattr, commands, "handle_rehash_request", commands.handle_rehash_request)
        # LIFO: release first, then join, then the excepthook comes back.
        case.addCleanup(setattr, threading, "excepthook", threading.excepthook)
        case.addCleanup(self.join, False)
        case.addCleanup(self.release.set)
        threading.excepthook = lambda args: self.raised.append(args.exc_type)

    def answer(self, value=None, raises=None, held=False):
        def rehash(*_args, **_kwargs):
            with self.lock:
                self.threads.append(threading.current_thread())
            self.entered.set()
            if held:
                self.release.wait(10)
            if raises is not None:
                raise raises
            return value
        commands.handle_rehash_request = rehash

    def started(self, count, limit=10.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.threads) >= count:
                    return True
            time.sleep(0.005)
        return False

    def join(self, check=True, count=1):
        if check:
            self.case.assertTrue(self.started(count), "the save started no rehash")
        with self.lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join(10)
            if check:
                self.case.assertFalse(thread.is_alive(), "the rehash thread did not finish")


def quietly(func, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

class TheRecord(unittest.TestCase):
    """runtime.SaveRehashOutcome on its own."""

    def setUp(self):
        self.record = runtime.SaveRehashOutcome()

    def test_before_any_save_there_is_nothing(self):
        self.assertEqual(self.record.snapshot(), {"seq": 0, "ok": None, "finished_at": None})

    def test_a_save_is_numbered_and_running_until_it_finishes(self):
        first = self.record.begin()
        second = self.record.begin()
        self.assertEqual((first, second), (1, 2))
        self.assertEqual(self.record.snapshot(), {"seq": 2, "ok": None, "finished_at": None})

    def test_finish_records_true_and_when(self):
        seq = self.record.begin()
        before = time.time()
        self.record.finish(seq, True)
        got = self.record.snapshot()
        self.assertEqual((got["seq"], got["ok"]), (seq, True))
        self.assertIsNotNone(got["finished_at"])
        self.assertGreaterEqual(got["finished_at"], before - 1)

    def test_only_an_explicit_true_is_true(self):
        # Fails closed, like the save's own `is True`: a truthy oddity or a
        # bare None from the rehash is not "in effect".
        for answer in (False, None, 1, "yes", object()):
            with self.subTest(answer=answer):
                seq = self.record.begin()
                self.record.finish(seq, answer)
                self.assertIs(self.record.snapshot()["ok"], False)

    def test_a_new_save_clears_the_last_outcome(self):
        self.record.finish(self.record.begin(), False)
        self.record.begin()
        got = self.record.snapshot()
        self.assertIsNone(got["ok"])
        self.assertIsNone(got["finished_at"])

    def test_an_older_rehash_finishing_late_does_not_answer_for_the_newer_save(self):
        older = self.record.begin()
        newer = self.record.begin()
        self.record.finish(older, True)
        self.assertEqual(self.record.snapshot(), {"seq": newer, "ok": None, "finished_at": None})
        self.record.finish(newer, False)
        self.assertIs(self.record.snapshot()["ok"], False)
        # and the late one still cannot overwrite it once it is known
        self.record.finish(older, True)
        self.assertEqual((self.record.snapshot()["seq"], self.record.snapshot()["ok"]), (newer, False))

    def test_a_number_no_save_was_given_is_ignored(self):
        # A rehash thread left over from before the record was replaced (a
        # test's reset, say) carries a number this record never handed out.
        seq = self.record.begin()
        self.record.finish(seq + 1, True)
        self.record.finish(seq + 7, False)
        self.assertEqual(self.record.snapshot(), {"seq": seq, "ok": None, "finished_at": None})

    def test_the_snapshot_is_a_copy(self):
        self.record.finish(self.record.begin(), True)
        got = self.record.snapshot()
        got["ok"] = False
        got["message"] = "changed by a caller"
        self.assertEqual(set(self.record.snapshot()), {"seq", "ok", "finished_at"})
        self.assertIs(self.record.snapshot()["ok"], True)

    def test_concurrent_saves_get_distinct_numbers(self):
        got, lock = [], threading.Lock()
        start = threading.Barrier(8)

        def save():
            start.wait(10)
            for _ in range(200):
                seq = self.record.begin()
                with lock:
                    got.append(seq)
        threads = [threading.Thread(target=save, daemon=True) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(got), list(range(1, 1601)))


class TheTestsStartWithAFreshRecord(DCCoreTestCase):
    """support.reset_config() replaces runtime.save_rehash: a save one test
    made is not the next test's."""

    def test_reset_gives_a_fresh_record(self):
        runtime.save_rehash.finish(runtime.save_rehash.begin(), False)
        support.reset_config()
        self.assertEqual(runtime.save_rehash.snapshot(), {"seq": 0, "ok": None, "finished_at": None})


# ---------------------------------------------------------------------------
# build_save_rehash_payload()
# ---------------------------------------------------------------------------

class ThePayload(DCCoreTestCase):

    def payload(self):
        return web_settings.build_save_rehash_payload()

    def test_before_any_save(self):
        self.assertEqual(self.payload(), {"seq": 0, "ok": None, "finished_at": None, "message": None})

    def test_while_the_rehash_runs_there_is_no_message(self):
        seq = runtime.save_rehash.begin()
        self.assertEqual(self.payload(), {"seq": seq, "ok": None, "finished_at": None, "message": None})

    def test_a_rehash_that_worked_has_no_message(self):
        seq = runtime.save_rehash.begin()
        runtime.save_rehash.finish(seq, True)
        got = self.payload()
        self.assertEqual((got["seq"], got["ok"], got["message"]), (seq, True, None))
        self.assertIsNotNone(got["finished_at"])

    def test_a_rehash_that_failed_says_the_shared_sentence(self):
        seq = runtime.save_rehash.begin()
        runtime.save_rehash.finish(seq, False)
        got = self.payload()
        self.assertEqual((got["seq"], got["ok"]), (seq, False))
        self.assertEqual(got["message"], web_settings.REHASH_FAILED_SAID)

    def test_it_is_the_one_webserver_serves(self):
        self.assertIs(webserver.build_save_rehash_payload, web_settings.build_save_rehash_payload)

    def test_it_does_not_wait_for_the_reload_lock(self):
        """The rehash it reports holds runtime.config_reload_lock while it
        reloads; the answer must not queue behind it."""
        held, done = threading.Event(), threading.Event()

        def holder():
            with runtime.config_reload_lock:
                held.set()
                done.wait(30)
        holding = threading.Thread(target=holder, daemon=True)
        holding.start()
        self.addCleanup(holding.join, 10)
        self.addCleanup(done.set)
        self.assertTrue(held.wait(10))

        # Control: the lock really is held by someone else right now.
        probe = []
        prober = threading.Thread(
            target=lambda: probe.append(runtime.config_reload_lock.acquire(timeout=0.05)), daemon=True)
        prober.start()
        prober.join(10)
        self.assertEqual(probe, [False], "the holder did not hold the reload lock")

        runtime.save_rehash.finish(runtime.save_rehash.begin(), False)
        answer = []
        asker = threading.Thread(target=lambda: answer.append(self.payload()), daemon=True)
        asker.start()
        asker.join(5)
        self.assertFalse(asker.is_alive(), "build_save_rehash_payload() waited for config_reload_lock")
        self.assertIs(answer[0]["ok"], False)


# ---------------------------------------------------------------------------
# The save records the outcome - with no on_applied, as the dashboard saves
# ---------------------------------------------------------------------------

class TheDashboardSaveRecordsTheOutcome(DCCoreTestCase):
    """web_settings.apply_settings_changes(body), exactly as POST
    /api/settings calls it: no source, no on_applied."""

    def setUp(self):
        super().setUp()
        self.rehash = RehashStandIn(self)

    def save(self, body=None):
        return quietly(web_settings.apply_settings_changes, {"MAX_DCC_SLOTS": "7"} if body is None else body)

    def saved_with(self, **answer):
        self.rehash.answer(**answer)
        status, result = self.save()
        self.assertEqual(status, 200, result)
        self.rehash.join()
        return result

    def test_each_answer_from_the_rehash(self):
        cases = (("True", {"value": True}, True), ("False", {"value": False}, False),
                 ("None", {"value": None}, False), ("raised", {"raises": CRASH}, False))
        for label, answer, ok in cases:
            with self.subTest(rehash=label):
                result = self.saved_with(**answer)
                got = web_settings.build_save_rehash_payload()
                self.assertEqual(got["seq"], result["rehash_seq"])
                self.assertIs(got["ok"], ok)
                self.assertIsNotNone(got["finished_at"])
                self.assertEqual(got["message"], None if ok else web_settings.REHASH_FAILED_SAID)
        # the raising one was not swallowed either
        self.assertEqual(self.rehash.raised, [RuntimeError])

    def test_each_save_gets_the_next_number(self):
        first = self.saved_with(value=True)["rehash_seq"]
        second = self.saved_with(value=True)["rehash_seq"]
        self.assertEqual(second, first + 1)

    def test_while_the_rehash_runs_the_outcome_is_unknown(self):
        self.rehash.answer(value=True, held=True)
        status, result = self.save()
        self.assertEqual(status, 200, result)
        self.assertTrue(self.rehash.entered.wait(10))
        got = web_settings.build_save_rehash_payload()
        self.assertEqual(got, {"seq": result["rehash_seq"], "ok": None, "finished_at": None, "message": None})
        self.rehash.release.set()
        self.rehash.join()
        self.assertIs(web_settings.build_save_rehash_payload()["ok"], True)

    def test_overlapping_saves_report_the_newer_ones_rehash(self):
        # Save A's rehash is held; save B's answers False at once. A then
        # finishes True, late: the record stays B's False.
        self.rehash.answer(value=True, held=True)
        _status, a = self.save()
        self.assertTrue(self.rehash.started(1))
        self.rehash.answer(value=False)
        _status, b = self.save({"MAX_DCC_SLOTS": "8"})
        self.assertEqual(b["rehash_seq"], a["rehash_seq"] + 1)
        self.assertTrue(self.rehash.started(2))
        # B's own thread finishes first
        with self.rehash.lock:
            b_thread = self.rehash.threads[1]
        b_thread.join(10)
        self.assertIs(web_settings.build_save_rehash_payload()["ok"], False)
        self.rehash.release.set()
        self.rehash.join(count=2)
        got = web_settings.build_save_rehash_payload()
        self.assertEqual((got["seq"], got["ok"]), (b["rehash_seq"], False))

    def test_a_refused_save_leaves_the_record_alone(self):
        before = self.saved_with(value=False)
        recorded = runtime.save_rehash.snapshot()
        self.assertEqual((recorded["seq"], recorded["ok"]), (before["rehash_seq"], False))
        for body in ({"MAX_DCC_SLOTS": "lots"},            # settings_file.save() refuses it
                     {"ADMIN_PASSWORD_HASH": "x"},          # refused before the save
                     {}):
            with self.subTest(body=body):
                status, result = self.save(body)
                self.assertEqual(status, 400, result)
                self.assertNotIn("rehash_seq", result)
                self.assertEqual(runtime.save_rehash.snapshot(), recorded)
        self.assertEqual(len(self.rehash.threads), 1, "a refused save started a rehash")

    def test_a_rehash_records_into_the_record_its_save_was_numbered_in(self):
        """The record is bound when the save starts. One replaced while the
        rehash runs (support.reset_config() does that between tests) is not
        told the outcome - even when it has handed out the same number."""
        self.rehash.answer(value=False, held=True)
        numbered_in = runtime.save_rehash
        status, result = self.save()
        self.assertEqual(status, 200, result)
        self.assertTrue(self.rehash.entered.wait(10))
        replacement = runtime.SaveRehashOutcome()
        self.addCleanup(setattr, runtime, "save_rehash", runtime.save_rehash)
        runtime.save_rehash = replacement
        self.assertEqual(replacement.begin(), result["rehash_seq"])
        self.rehash.release.set()
        self.rehash.join()
        self.assertEqual(replacement.snapshot(), {"seq": result["rehash_seq"], "ok": None, "finished_at": None})
        # control: the outcome was recorded, in the record that numbered it
        got = numbered_in.snapshot()
        self.assertEqual((got["seq"], got["ok"]), (result["rehash_seq"], False))

    def test_the_answer_says_how_long_the_rehash_may_wait(self):
        self.set_config(REHASH_TRANSFER_WAIT=17)
        result = self.saved_with(value=True)
        self.assertEqual(result["rehash"], "started")
        self.assertEqual(result["rehash_wait"], 17)


class OnAppliedAndTheRecordDoNotStopEachOther(DCCoreTestCase):
    """The console's path: the record and on_applied are both told, and
    either one raising does not keep the other from it."""

    def setUp(self):
        super().setUp()
        self.rehash = RehashStandIn(self)
        self.told = []
        self.called = threading.Event()

    def save(self, on_applied):
        status, result = quietly(web_settings.apply_settings_changes, {"MAX_DCC_SLOTS": "7"},
                                 on_applied=on_applied)
        self.assertEqual(status, 200, result)
        return result

    def test_a_raising_on_applied_does_not_lose_the_record(self):
        def on_applied(ok):
            self.told.append(ok)
            self.called.set()
            raise RuntimeError("example on_applied crash")
        self.rehash.answer(value=False)
        result = self.save(on_applied)
        self.rehash.join()
        self.assertEqual(self.told, [False])
        got = runtime.save_rehash.snapshot()
        self.assertEqual((got["seq"], got["ok"]), (result["rehash_seq"], False))

    def test_a_raising_record_does_not_stop_on_applied(self):
        def broken_finish(_seq, _ok):
            raise RuntimeError("example record crash")
        runtime.save_rehash.finish = broken_finish       # this test's fresh record only

        def on_applied(ok):
            self.told.append(ok)
            self.called.set()
        self.rehash.answer(value=True)
        self.save(on_applied)
        self.rehash.join()
        self.assertTrue(self.called.wait(10), "on_applied was never called")
        self.assertEqual(self.told, [True])
        self.assertIn(RuntimeError, self.rehash.raised)  # not swallowed


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_FLASK, NO_FLASK)
class TheRoute(RouteCase):

    def setUp(self):
        super().setUp()
        self.rehash = RehashStandIn(self)

    def post_save(self, body=None):
        resp = quietly(self.client.post, "/api/settings", json=body or {"MAX_DCC_SLOTS": "7"})
        return resp.status_code, resp.get_json()

    def outcome(self):
        resp = self.client.get("/api/settings/rehash")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_it_reports_what_the_dashboards_save_rehash_did(self):
        self.log_in()
        cases = (("True", {"value": True}, True), ("False", {"value": False}, False),
                 ("None", {"value": None}, False), ("raised", {"raises": CRASH}, False))
        for label, answer, ok in cases:
            with self.subTest(rehash=label):
                self.rehash.answer(**answer)
                status, saved = self.post_save()
                self.assertEqual(status, 200, saved)
                self.assertIsInstance(saved["rehash_seq"], int)
                self.rehash.join()
                got = self.outcome()
                self.assertEqual(set(got), {"seq", "ok", "finished_at", "message"})
                self.assertEqual(got["seq"], saved["rehash_seq"])
                self.assertIs(got["ok"], ok)
                if ok:
                    self.assertIsNone(got["message"])
                else:
                    self.assertEqual(got["message"], webserver.REHASH_FAILED_SAID)

    def test_running_then_known(self):
        self.log_in()
        self.rehash.answer(value=False, held=True)
        status, saved = self.post_save()
        self.assertEqual(status, 200, saved)
        self.assertTrue(self.rehash.entered.wait(10))
        self.assertEqual(self.outcome(), {"seq": saved["rehash_seq"], "ok": None,
                                          "finished_at": None, "message": None})
        self.rehash.release.set()
        self.rehash.join()
        self.assertIs(self.outcome()["ok"], False)

    def test_a_refused_save_through_the_route_changes_nothing(self):
        self.log_in()
        self.rehash.answer(value=True)
        self.post_save()
        self.rehash.join()
        before = self.outcome()
        status, refused = self.post_save({"MAX_DCC_SLOTS": "lots"})
        self.assertEqual(status, 400, refused)
        self.assertEqual(self.outcome(), before)

    def test_the_save_answer_carries_the_wait(self):
        self.set_config(REHASH_TRANSFER_WAIT=42)
        self.log_in()
        self.rehash.answer(value=True)
        status, saved = self.post_save()
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["rehash_wait"], 42)
        self.rehash.join()

    def test_it_is_behind_the_login(self):
        resp = self.client.get("/api/settings/rehash")
        self.assertEqual(resp.status_code, 401)
        self.log_in()
        self.assertEqual(self.client.get("/api/settings/rehash").status_code, 200)

    def test_it_answers_get_only(self):
        self.log_in()
        self.assertEqual(self.client.post("/api/settings/rehash").status_code, 405)
        rules = [rule for rule in self.app.url_map.iter_rules() if str(rule) == "/api/settings/rehash"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].methods - {"HEAD", "OPTIONS"}, {"GET"})


# ---------------------------------------------------------------------------
# One sentence: the console's and the dashboard's
# ---------------------------------------------------------------------------

class TheConsoleSaysTheSameSentence(ConsoleCase):
    """The console's SETAPPLYERR line and plain sentence, run for real, are
    the dashboard's sentence."""

    def failed_save(self, structured, slots=4):
        session = make_session(self, structured=structured)
        self.rehash_answers(False)
        reply = run(session, "set MAX_DCC_SLOTS %d" % slots)
        line = self.wait_for(session, lambda x: x not in reply)
        self.assertIsNotNone(line, session.sent)
        self.finished()
        return line

    def test_the_structured_line_carries_the_shared_why(self):
        line = self.failed_save(structured=True)
        self.assertEqual(line, "DCCORE SETAPPLYERR " + webserver.REHASH_FAILED_WHY)

    def test_the_plain_sentence_is_the_dashboards_message(self):
        line = self.failed_save(structured=False)
        self.assertEqual(line, webserver.REHASH_FAILED_SAID)
        # and the console's save was numbered too: the page asked now would
        # be told the same words
        self.assertEqual(web_settings.build_save_rehash_payload()["message"], line)

    def test_the_console_reads_the_shared_sentence_rather_than_its_own_copy(self):
        self.addCleanup(setattr, webserver, "REHASH_FAILED_WHY", webserver.REHASH_FAILED_WHY)
        self.addCleanup(setattr, webserver, "REHASH_FAILED_SAID", webserver.REHASH_FAILED_SAID)
        webserver.REHASH_FAILED_WHY = "example why sentence"
        webserver.REHASH_FAILED_SAID = "example said sentence"
        self.assertEqual(self.failed_save(structured=True), "DCCORE SETAPPLYERR example why sentence")
        self.release.clear()
        self.threads = []
        self.assertEqual(self.failed_save(structured=False, slots=5), "example said sentence")

    def test_webserver_re_exports_the_one_from_web_settings(self):
        self.assertIs(webserver.REHASH_FAILED_WHY, web_settings.REHASH_FAILED_WHY)
        self.assertIs(webserver.REHASH_FAILED_SAID, web_settings.REHASH_FAILED_SAID)
        self.assertTrue(web_settings.REHASH_FAILED_SAID.endswith(web_settings.REHASH_FAILED_WHY))
        self.assertIn("settings.conf", web_settings.REHASH_FAILED_SAID)
        self.assertNotIn("in effect", web_settings.REHASH_FAILED_SAID)
        self.assertNotIn("\n", web_settings.REHASH_FAILED_SAID)

    def test_the_payload_reads_the_shared_sentence(self):
        self.addCleanup(setattr, web_settings, "REHASH_FAILED_SAID", web_settings.REHASH_FAILED_SAID)
        web_settings.REHASH_FAILED_SAID = "example said sentence"
        runtime.save_rehash.finish(runtime.save_rehash.begin(), False)
        self.assertEqual(web_settings.build_save_rehash_payload()["message"], "example said sentence")


# ---------------------------------------------------------------------------
# The Settings page, under node
# ---------------------------------------------------------------------------

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
// The page's own constants, as declared beside the functions that use them.
const consts = src.match(/^\s*var SETTINGS_REHASH_[A-Z_]+ = [^;]+;/gm);
if (!consts || consts.length < 2) { throw new Error("the rehash poll's constants were not found"); }

// A fake clock and timers: time moves only when a timer is run.
let now = 0;
let timers = [];
const FakeDate = { now: function () { return now; } };
function fakeSetTimeout(f, ms) { timers.push({ at: now + (ms || 0), f: f }); return timers.length; }

// The three notes under the Save bar, as the page's own showSettingsStatus()
// leaves them, and a log of every call to it.
function note(name) { return { name: name, style: { display: "none" }, textContent: "", className: "" }; }
let el = null;
let state = {};
let shown = [];
let fetched = [];
let posts = [];
let loads = 0;
let server = null;           // function(url) -> value | {pending: true} | {reject: true}
let pending = [];
let postAnswer = null;       // what POST /api/settings answers; {reject: true} = no answer at all
let editor = null;           // what servedDraftUnsaved() says: null, "folders" or "lists"
let editorSaves = [];
function t(key) { return key; }
const realShow = new Function(fn("function showSettingsStatus(") + "\nreturn showSettingsStatus;")();
function showSettingsStatus(node, text, isError) {
  shown.push({ node: node.name, text: text, error: !!isError });
  realShow(node, text, isError);
}
function loadSettings() { loads++; }
function updateSettingsSaveBar() {}
function postJson(url) {
  posts.push(url);
  if (postAnswer && postAnswer.reject) { return Promise.reject(new Error("network down")); }
  return Promise.resolve(postAnswer);
}
function fetchJson(url) {
  fetched.push(url);
  const answer = server(url);
  if (answer && answer.pending) {
    return new Promise(function (resolve, reject) { pending.push({ resolve: resolve, reject: reject }); });
  }
  if (answer && answer.reject) { return Promise.reject(new Error("HTTP 500")); }
  return Promise.resolve(answer);
}
function servedDraftUnsaved() { return editor; }
// The folder or list editor's own save: accepted, and its note set, as
// saveFolders()/saveLists() leave it for saveSettingsPage() to show.
function editorSave(which) {
  return function () {
    editorSaves.push(which);
    state[which + "Note"] = { ok: true, text: which + ".savedNote" };
    return Promise.resolve(true);
  };
}
const fakeWindow = { confirm: function () { throw new Error("nothing here clears the debug channel"); } };

const page = new Function("state", "el", "t", "fetchJson", "postJson", "showSettingsStatus", "loadSettings",
                          "updateSettingsSaveBar", "setTimeout", "Date",
                          "servedDraftUnsaved", "saveFolders", "saveLists", "window",
  consts.join("\n") + "\n" + fn("function awaitSettingsRehash(") + "\n" + fn("function saveTheSettings(") +
  "\n" + fn("function saveSettingsPage(") +
  "\nreturn { awaitSettingsRehash: awaitSettingsRehash, saveTheSettings: saveTheSettings, " +
  "saveSettingsPage: saveSettingsPage };");

function fresh() {
  now = 0; timers = []; shown = []; fetched = []; posts = []; loads = 0; pending = []; server = null;
  postAnswer = null; editor = null; editorSaves = [];
  el = { settingsSaveBtn: { disabled: false }, settingsSaveStatus: note("save"),
         settingsRestartNote: note("restart"), settingsRehashNote: note("rehash"),
         settingsSavebarText: { textContent: "" } };
  state = { settingsDirty: {} };
  return page(state, el, t, fetchJson, postJson, showSettingsStatus, loadSettings,
              updateSettingsSaveBar, fakeSetTimeout, FakeDate,
              servedDraftUnsaved, editorSave("folders"), editorSave("lists"), fakeWindow);
}
function settle() { return new Promise(function (resolve) { setImmediate(resolve); }); }
// Run the earliest timer, then let its promises settle.
async function step() {
  if (!timers.length) { return false; }
  timers.sort(function (a, b) { return a.at - b.at; });
  const next = timers.shift();
  now = Math.max(now, next.at);
  next.f();
  await settle();
  return true;
}
async function drain(limit) {
  let n = 0;
  while (n < (limit || 1000) && await step()) { n++; }
  return n;
}
function noteState(node) {
  return { text: node.textContent, display: node.style.display, error: node.className.indexOf("is-error") >= 0 };
}
function record() {
  return { fetched: fetched.length, shown: shown.slice(), loads: loads, timers: timers.length, posts: posts.slice(),
           now: now, waiting: state.settingsRehashWait === null ? null : typeof state.settingsRehashWait,
           notes: { save: noteState(el.settingsSaveStatus), rehash: noteState(el.settingsRehashNote),
                    restart: noteState(el.settingsRestartNote) } };
}

(async function () {
  const out = {};
  let p;

  // No rehash_seq: nothing to wait for.
  p = fresh();
  server = function () { return { seq: 1, ok: true }; };
  p.awaitSettingsRehash({ rehash: "started", written: ["MAX_DCC_SLOTS"] });
  p.awaitSettingsRehash(undefined);
  p.awaitSettingsRehash({ rehash_seq: "3" });
  await drain();
  out.noSeq = record();

  // The same through the save: a 200 with no number polls nothing.
  p = fresh();
  server = function () { return { seq: 1, ok: true }; };
  postAnswer = { ok: true, status: 200, data: { written: ["MAX_DCC_SLOTS"], restart_required: [] } };
  p.saveTheSettings({ MAX_DCC_SLOTS: "7" }, false);
  await settle();
  await drain();
  out.saveNoSeq = record();

  // Keeps asking while the record is an older save's, or still running.
  p = fresh();
  const answers = [{ seq: 4, ok: true }, { seq: 4, ok: false, message: "an older save" },
                   { seq: 5, ok: null }, { reject: true }, { seq: 5, ok: null }, { seq: 5, ok: true }];
  server = function () { return answers.shift() || { seq: 5, ok: true }; };
  p.awaitSettingsRehash({ rehash_seq: 5, rehash_wait: 120 });
  out.waitingBefore = { fetched: fetched.length, timers: timers.length };
  const polls = [];
  for (let i = 0; i < 5; i++) { await step(); polls.push({ fetched: fetched.length, shown: shown.length, now: now }); }
  out.polling = { polls: polls };
  await step();
  out.polling.done = record();
  await drain();
  out.polling.after = record();

  // A later save's answer counts as this one's.
  p = fresh();
  server = function () { return { seq: 9, ok: true }; };
  p.awaitSettingsRehash({ rehash_seq: 5, rehash_wait: 0 });
  await drain();
  out.later = record();

  // False: the error, with the server's own sentence.
  p = fresh();
  server = function () { return { seq: 5, ok: false, message: "The example sentence." }; };
  p.awaitSettingsRehash({ rehash_seq: 5, rehash_wait: 0 });
  await drain();
  out.failed = record();

  // Never known: the cap is 2 x rehash_wait + the margin, and the margin
  // alone when the wait is missing, negative or not a number.
  out.caps = {};
  const capCases = { wait10: { rehash_seq: 5, rehash_wait: 10 }, wait30: { rehash_seq: 5, rehash_wait: 30 },
                     missing: { rehash_seq: 5 }, negative: { rehash_seq: 5, rehash_wait: -10 },
                     text: { rehash_seq: 5, rehash_wait: "soon" } };
  for (const label of Object.keys(capCases)) {
    p = fresh();
    server = function () { return { seq: 5, ok: null }; };
    p.awaitSettingsRehash(capCases[label]);
    await drain(10000);
    out.caps[label] = record();
  }

  // A save that named settings needing a restart: "in effect" does not
  // claim those. A failure is a failure either way.
  out.restart = {};
  const restartCases = { named: [["WEBUI_PORT"], true], empty: [[], true], missing: [undefined, true],
                         failedNamed: [["WEBUI_PORT"], false] };
  for (const label of Object.keys(restartCases)) {
    const named = restartCases[label][0], ok = restartCases[label][1];
    p = fresh();
    server = function () { return ok ? { seq: 1, ok: true } : { seq: 1, ok: false, message: "The example sentence." }; };
    const saved = { rehash_seq: 1, rehash_wait: 0 };
    if (named !== undefined) { saved.restart_required = named; }
    p.awaitSettingsRehash(saved);
    await drain();
    out.restart[label] = record();
  }
  // ... and the same through the save itself, beside its restart note.
  p = fresh();
  server = function () { return { seq: 1, ok: true }; };
  postAnswer = { ok: true, status: 200, data: { written: ["WEBUI_PORT"], restart_required: ["WEBUI_PORT"],
                                                rehash_seq: 1, rehash_wait: 0 } };
  p.saveTheSettings({ WEBUI_PORT: "8081" }, false);
  await settle();
  await drain();
  out.restartThroughSave = record();

  // A newer accepted save ends the older wait - even with its poll in flight.
  p = fresh();
  server = function () { return { pending: true }; };
  postAnswer = { ok: true, status: 200,
                 data: { written: ["MAX_DCC_SLOTS"], restart_required: [], rehash_seq: 2, rehash_wait: 0 } };
  p.awaitSettingsRehash({ rehash_seq: 1, rehash_wait: 0 });
  await step();                                    // the older save's poll is in flight
  out.newer = { inFlight: pending.length };
  p.saveTheSettings({ MAX_DCC_SLOTS: "8" }, false);
  await settle();
  await step();                                    // the newer save's poll is in flight too
  out.newer.inFlightBoth = pending.length;
  const loadsBefore = loads;
  pending[0].resolve({ seq: 2, ok: false, message: "answered to the older wait" });
  await settle();
  out.newer.afterOlder = { shown: shown.slice(), loads: loads - loadsBefore, timers: timers.length };
  if (pending[1]) { pending[1].resolve({ seq: 2, ok: true }); }   // absent if the save never asked
  await settle();
  await drain();
  out.newer.final = record();
  out.newer.loadsFromPolls = loads - loadsBefore;

  // An accepted save with no rehash_seq still ends the older wait ...
  p = fresh();
  server = function () { return { pending: true }; };
  p.awaitSettingsRehash({ rehash_seq: 1, rehash_wait: 0 });
  await step();
  out.acceptedNoSeq = { inFlight: pending.length };
  postAnswer = { ok: true, status: 200, data: { written: ["MAX_DCC_SLOTS"], restart_required: [] } };
  p.saveTheSettings({ MAX_DCC_SLOTS: "8" }, false);
  await settle();
  out.acceptedNoSeq.atSave = record();
  pending[0].resolve({ seq: 1, ok: false, message: "answered to the older wait" });
  await settle();
  await drain();
  out.acceptedNoSeq.final = record();

  // ... and hides the older outcome's note.
  p = fresh();
  server = function () { return { seq: 1, ok: false, message: "The example sentence." }; };
  p.awaitSettingsRehash({ rehash_seq: 1, rehash_wait: 0 });
  await drain();
  out.acceptedNoSeqHides = { before: record() };
  postAnswer = { ok: true, status: 200, data: { written: ["MAX_DCC_SLOTS"], restart_required: [] } };
  p.saveTheSettings({ MAX_DCC_SLOTS: "8" }, false);
  await settle();
  await drain();
  out.acceptedNoSeqHides.after = record();

  // A newer save the server refused (a 400), or never answered, starts no
  // rehash: the older wait runs on, and its outcome lands in its own note
  // while the refusal stays where it was said.
  out.keptWaiting = {};
  for (const how of ["refused", "rejected"]) {
    p = fresh();
    let released = false;
    server = function () {
      if (!released) { return { seq: 1, ok: null }; }
      return how === "refused" ? { seq: 1, ok: true } : { seq: 1, ok: false, message: "The example sentence." };
    };
    p.awaitSettingsRehash({ rehash_seq: 1, rehash_wait: 0 });
    await step();
    const askedBefore = fetched.length;
    postAnswer = how === "refused"
      ? { ok: false, status: 400, data: { error: "MAX_DCC_SLOTS: expected a whole number, got 'lots'" } }
      : { reject: true };
    p.saveTheSettings({ MAX_DCC_SLOTS: "lots" }, false);
    await settle();
    const atRefusal = record();
    await step();
    await step();
    const askedAfter = fetched.length;
    released = true;
    await drain();
    out.keptWaiting[how] = { askedBefore: askedBefore, atRefusal: atRefusal, askedAfter: askedAfter,
                             final: record() };
  }

  // A finished outcome answers an earlier save: a success is taken down when
  // a newer save starts, so a refusal of that one is not read under it; a
  // failure stays, since it is still true.
  out.staleAfterRefusal = {};
  for (const how of ["success", "failure"]) {
    p = fresh();
    server = function () {
      return how === "success" ? { seq: 1, ok: true } : { seq: 1, ok: false, message: "The example sentence." };
    };
    p.awaitSettingsRehash({ rehash_seq: 1, rehash_wait: 0 });
    await drain();
    const before = record();
    postAnswer = { ok: false, status: 400, data: { error: "MAX_DCC_SLOTS: expected a whole number, got 'lots'" } };
    p.saveTheSettings({ MAX_DCC_SLOTS: "lots" }, false);
    await settle();
    out.staleAfterRefusal[how] = { before: before, after: record() };
  }

  // A save of the folder or list editor alone, through the page's one Save
  // button, starts no rehash either: the older wait runs on, and its outcome
  // does not cover the editor's note.
  out.editorOnly = {};
  for (const which of ["folders", "lists"]) {
    p = fresh();
    let released = false;
    server = function () { return released ? { seq: 1, ok: true } : { seq: 1, ok: null }; };
    p.awaitSettingsRehash({ rehash_seq: 1, rehash_wait: 0 });
    await step();
    editor = which;
    p.saveSettingsPage();
    await settle();
    const atSave = record();
    await step();
    const askedAfter = fetched.length;
    released = true;
    await drain();
    out.editorOnly[which] = { atSave: atSave, askedAfter: askedAfter, editorSaves: editorSaves.slice(),
                              final: record() };
  }

  console.log(JSON.stringify(out));
})().catch(function (err) { console.error(err && err.stack || err); process.exit(1); });
"""

_RESULT = []
NODE = shutil.which("node")


def run_the_page(app_js=None):
    """The harness's findings for web/app.js (or `app_js`, a path)."""
    handle, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(HARNESS)
        done = subprocess.run([NODE, path, app_js or os.path.join(REPO_ROOT, "web", "app.js")],
                              capture_output=True, timeout=120)
    finally:
        os.unlink(path)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return json.loads(done.stdout.decode("utf-8"))


def outcome(text, error=False):
    """What the page shows in its rehash note."""
    return {"node": "rehash", "text": text, "error": error}


@unittest.skipUnless(NODE, "node is not installed; CI's runners have it")
class TheSettingsPageWaitsForTheOutcome(unittest.TestCase):

    # The poll's interval, in ms: a capped wait gives up at most one past it.
    POLL_MS = 1500

    @classmethod
    def setUpClass(cls):
        if not _RESULT:
            _RESULT.append(run_the_page())
        cls.result = _RESULT[0]

    def rehash_notes(self, got):
        return [s for s in got["shown"] if s["node"] == "rehash"]

    def test_no_number_no_poll(self):
        got = self.result["noSeq"]
        self.assertEqual((got["fetched"], got["timers"], got["shown"], got["loads"]), (0, 0, [], 0))
        got = self.result["saveNoSeq"]
        self.assertEqual(got["fetched"], 0)
        # the save itself said what it always said, and loaded the page once
        self.assertEqual(got["shown"], [{"node": "save", "text": "settings.savedSettingsOne", "error": False}])
        self.assertEqual(got["loads"], 1)
        self.assertEqual(got["notes"]["rehash"]["display"], "none")

    def test_nothing_is_asked_before_the_first_interval(self):
        self.assertEqual(self.result["waitingBefore"], {"fetched": 0, "timers": 1})

    def test_it_keeps_asking_while_the_answer_is_not_its_own(self):
        polls = self.result["polling"]["polls"]
        # an older save's True and False, a running one, a failed request,
        # running again: five asks, nothing said, one interval apart
        self.assertEqual([p["fetched"] for p in polls], [1, 2, 3, 4, 5])
        self.assertEqual([p["shown"] for p in polls], [0] * 5)
        gaps = {b["now"] - a["now"] for a, b in zip(polls, polls[1:])}
        self.assertEqual(gaps, {self.POLL_MS}, polls)

    def test_then_it_says_in_effect_in_its_own_note_and_reloads_the_page(self):
        done = self.result["polling"]["done"]
        self.assertEqual(done["fetched"], 6)
        self.assertEqual(done["shown"], [outcome("settings.rehashApplied")])
        self.assertEqual(done["notes"]["rehash"],
                         {"text": "settings.rehashApplied", "display": "block", "error": False})
        self.assertEqual(done["notes"]["save"]["display"], "none")
        self.assertEqual(done["loads"], 1)
        self.assertIsNone(done["waiting"])
        after = self.result["polling"]["after"]
        self.assertEqual((after["fetched"], after["timers"], len(after["shown"])), (6, 0, 1))

    def test_a_later_saves_answer_is_taken(self):
        got = self.result["later"]
        self.assertEqual(got["fetched"], 1)
        self.assertEqual(got["shown"], [outcome("settings.rehashApplied")])

    def test_a_failure_is_an_error_with_the_servers_sentence(self):
        got = self.result["failed"]
        self.assertEqual(got["shown"], [outcome("settings.rehashFailed The example sentence.", error=True)])
        self.assertTrue(got["notes"]["rehash"]["error"])
        self.assertEqual(got["loads"], 1)
        self.assertEqual(got["timers"], 0)

    def test_at_the_cap_it_says_not_finished_and_stops(self):
        # 2 x rehash_wait + 60 s; 60 s when the wait is missing, negative or
        # not a number
        caps = {"wait10": 80, "wait30": 120, "missing": 60, "negative": 60, "text": 60}
        self.assertEqual(set(self.result["caps"]), set(caps))
        for label, seconds in caps.items():
            with self.subTest(rehash_wait=label):
                got = self.result["caps"][label]
                self.assertEqual(got["shown"], [outcome("settings.rehashNotFinished")])
                self.assertEqual(got["timers"], 0, "it kept asking after the cap")
                self.assertEqual(got["loads"], 0)
                self.assertIsNone(got["waiting"])
                # not before the cap, and no more than one interval after it
                self.assertGreater(got["now"], seconds * 1000)
                self.assertLessEqual(got["now"], seconds * 1000 + self.POLL_MS)
                # it asked all the way there
                self.assertEqual(got["fetched"], got["now"] // self.POLL_MS - 1)

    def test_a_save_naming_restart_settings_says_those_are_not_in_effect(self):
        restart = self.result["restart"]
        self.assertEqual(restart["named"]["shown"], [outcome("settings.rehashAppliedRestart")])
        self.assertEqual(restart["empty"]["shown"], [outcome("settings.rehashApplied")])
        self.assertEqual(restart["missing"]["shown"], [outcome("settings.rehashApplied")])
        self.assertEqual(restart["failedNamed"]["shown"],
                         [outcome("settings.rehashFailed The example sentence.", error=True)])
        # through the save: the restart note names them, the rehash note does
        # not claim them
        got = self.result["restartThroughSave"]
        self.assertEqual(got["shown"], [
            {"node": "save", "text": "settings.savedSettingsOne", "error": False},
            {"node": "restart", "text": "settings.restartRequiredToApply", "error": True},
            outcome("settings.rehashAppliedRestart"),
        ])
        self.assertEqual(got["notes"]["restart"]["display"], "block")
        self.assertEqual(got["notes"]["save"]["text"], "settings.savedSettingsOne")

    def test_a_newer_save_ends_the_older_wait(self):
        got = self.result["newer"]
        self.assertEqual((got["inFlight"], got["inFlightBoth"]), (1, 2))
        # the older wait's answer arrives after the newer save: ignored
        self.assertEqual(got["afterOlder"]["loads"], 0)
        self.assertEqual(self.rehash_notes(got["afterOlder"]), [])
        self.assertEqual(got["afterOlder"]["timers"], 0)
        final = got["final"]
        self.assertEqual(self.rehash_notes(final), [outcome("settings.rehashApplied")])
        self.assertEqual(got["loadsFromPolls"], 1)   # the newer outcome's reload only
        self.assertEqual(final["timers"], 0)

    def test_an_accepted_save_with_no_number_still_ends_the_older_wait(self):
        got = self.result["acceptedNoSeq"]
        self.assertEqual(got["inFlight"], 1)
        self.assertIsNone(got["atSave"]["waiting"])
        final = got["final"]
        self.assertEqual(final["fetched"], 1, "it asked again for the older save")
        self.assertEqual(self.rehash_notes(final), [])
        self.assertEqual(final["notes"]["rehash"]["display"], "none")
        self.assertEqual(final["loads"], 1)             # the save's own reload only
        self.assertEqual(final["timers"], 0)
        self.assertEqual(final["notes"]["save"]["text"], "settings.savedSettingsOne")

    def test_an_accepted_save_hides_the_older_outcome(self):
        got = self.result["acceptedNoSeqHides"]
        # control: the older outcome was on show
        self.assertEqual(got["before"]["notes"]["rehash"]["display"], "block")
        self.assertTrue(got["before"]["notes"]["rehash"]["error"])
        self.assertEqual(got["after"]["notes"]["rehash"]["display"], "none")
        self.assertEqual(len(self.rehash_notes(got["after"])), 1)   # the older one's, nothing new

    def test_a_refused_or_unanswered_newer_save_leaves_the_older_wait_running(self):
        refusals = {"refused": "MAX_DCC_SLOTS: expected a whole number, got 'lots'",
                    "rejected": "settings.couldNotReachDashboard"}
        outcomes = {"refused": outcome("settings.rehashApplied"),
                    "rejected": outcome("settings.rehashFailed The example sentence.", error=True)}
        for how, refusal in refusals.items():
            with self.subTest(newer_save=how):
                got = self.result["keptWaiting"][how]
                at = got["atRefusal"]
                self.assertEqual(at["posts"], ["/api/settings"])
                self.assertEqual(at["waiting"], "object", "the refusal ended the older wait")
                self.assertEqual(at["notes"]["save"], {"text": refusal, "display": "block", "error": True})
                # it went on asking for the older save
                self.assertEqual(got["askedAfter"], got["askedBefore"] + 2)
                final = got["final"]
                self.assertEqual(self.rehash_notes(final), [outcomes[how]])
                self.assertEqual(final["notes"]["rehash"]["display"], "block")
                # and the refusal is still what the save's note says
                self.assertEqual(final["notes"]["save"], {"text": refusal, "display": "block", "error": True})
                self.assertEqual([s["node"] for s in final["shown"]], ["save", "rehash"])
                self.assertEqual(final["loads"], 1)          # the outcome's reload; the refusal loads nothing
                self.assertEqual(final["timers"], 0)

    def test_a_finished_success_is_taken_down_when_a_newer_save_is_refused(self):
        """Left up, the green "in effect" of an earlier save would sit under
        this save's refusal and read as its answer. A failure stays: the
        bot may still not run what was saved."""
        refusal = {"text": "MAX_DCC_SLOTS: expected a whole number, got 'lots'", "display": "block", "error": True}
        for how, stays in (("success", False), ("failure", True)):
            with self.subTest(earlier=how):
                got = self.result["staleAfterRefusal"][how]
                self.assertEqual(got["before"]["notes"]["rehash"]["display"], "block", "control: it was shown")
                self.assertEqual(got["after"]["notes"]["save"], refusal)
                self.assertEqual(got["after"]["notes"]["rehash"]["display"], "block" if stays else "none")

    def test_a_folders_or_lists_only_save_leaves_the_older_wait_running(self):
        for which in ("folders", "lists"):
            with self.subTest(editor=which):
                got = self.result["editorOnly"][which]
                self.assertEqual(got["editorSaves"], [which])
                at = got["atSave"]
                self.assertEqual(at["posts"], [], "an editor-only save posted the settings")
                self.assertEqual(at["waiting"], "object", "the editor's save ended the older wait")
                self.assertEqual(at["notes"]["save"],
                                 {"text": which + ".savedNote", "display": "block", "error": False})
                self.assertEqual(got["askedAfter"], at["fetched"] + 1)
                final = got["final"]
                self.assertEqual(self.rehash_notes(final), [outcome("settings.rehashApplied")])
                # the editor's note is not overwritten by the outcome
                self.assertEqual(final["notes"]["save"],
                                 {"text": which + ".savedNote", "display": "block", "error": False})
                self.assertEqual(final["timers"], 0)


if __name__ == "__main__":
    unittest.main()
