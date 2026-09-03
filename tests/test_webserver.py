"""webserver.py's build_*_payload() functions.

Deliberately exercises only the pure, Flask-free half of webserver.py:
build_queue_payload(), build_search_payload() and build_filelists_payload()
never import flask, which is what lets this file run - like the rest of the
suite - with nothing but the standard library. create_app()/start() are the
Flask-gated half and are only smoke-tested here for the "Flask is missing"
and "disabled via config" paths, which must never raise regardless of whether
Flask happens to be installed in the environment running this file.
"""

import ast
import importlib
import io
import os
import sys
import threading
import time
import unittest
from contextlib import nullcontext, redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import adminchat  # noqa: E402
import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import db  # noqa: E402
import list as list_mod  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, queue_row, silence_debug  # noqa: E402

WEBUI_TEST_PASSWORD = "test-password"


def log_in_test_client(client, password=WEBUI_TEST_PASSWORD):
    """POST the login form on a Flask test client, the same way a browser
    would - so a protected route's test reaches the code under test instead
    of the 401/redirect require_login() now puts in front of every route.
    Low iteration count purely for test speed; see test_adminchat.py for the
    same pattern.

    Clears webserver._web_bad_ips first: every Flask test client presents the
    same synthetic remote_addr, so a block left over from an earlier test in
    the same process (module-level state, not reset between tests otherwise)
    would fail a login this call has no reason to expect to fail."""
    webserver._web_bad_ips.clear()
    resp = client.post("/login", data={"password": password})
    assert resp.status_code == 302, "test login failed - fixture's own password/hash disagree"
    return resp


def write_master_list(lists_dir, base_name, folders):
    """A minimal master list in update_list.py's exact on-disk shape.

    `folders` is [(folder_path_or_None, [(filename, size_str), ...]), ...],
    mirroring the "====...====" / folder / "====...====" header block
    update_list.py writes before each folder's "!..." lines
    (update_list.py:190-195, :216).
    """
    path = os.path.join(lists_dir, f"{base_name}-2026-08-25.txt")
    rule = "=" * 53
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("List of 2 Files (1.0MB) generated on Aug 25th\n")
        f.write(f"To request a file, copy/paste to the channel... !{base_name} FILENAME\n\n\n")
        for folder, files in folders:
            if folder:
                f.write(f"\n{rule}\n{folder}\n{rule}\n")
            for filename, size in files:
                f.write(f"!{base_name} {filename}  ::INFO:: {size}\n")
    return path


def _quiet_reload_config():
    """importlib.reload(config), with its own "[CONFIG] Applied N setting(s)"
    print() suppressed - the same pattern tests/test_runtime_state.py uses
    for the same call. Used below as a safe, config.py-only stand-in for what
    a real rehash would do to pick a freshly-saved setting.conf value back up
    (real commands.handle_rehash_request() additionally reloads dcc/announce/
    security/db/stats_mgr, none of which any test in this suite reloads for
    real - this suite's only precedent, test_runtime_state.py, reloads
    config.py alone too, for the same reason), and again in cleanup to put
    every setting back at its tracked default once the temp settings.conf
    that changed it is gone (DCCoreTestCase.tearDown() deletes it before any
    addCleanup callback runs, so a reload at that point sees no override file
    at all)."""
    with redirect_stdout(io.StringIO()):
        importlib.reload(config)


class QueuePayloadTests(DCCoreTestCase):

    def test_summary_rows_reflect_sending_frozen_and_queued(self):
        config.dcc_queue = {
            "alice": [queue_row(user="alice", filename="A.flac")],
            "bob":   [queue_row(user="bob", filename="B.flac"), queue_row(user="bob", filename="C.flac")],
            "carol": [queue_row(user="carol", filename="D.flac")],
        }
        config.frozen_queues = {"bob": 12345.0}
        config.active_transfers = [{"user": "carol", "file": "D.flac"}]

        rows = {row["user"]: row for row in webserver.build_queue_payload()}

        self.assertEqual(rows["alice"]["status"], "queued")
        self.assertEqual(rows["alice"]["preview"], "A.flac")
        self.assertEqual(rows["alice"]["count"], 1)

        self.assertEqual(rows["bob"]["status"], "frozen")
        self.assertEqual(rows["bob"]["count"], 2)

        self.assertEqual(rows["carol"]["status"], "sending")

    def test_sending_status_wins_over_frozen(self):
        """A user can be frozen and still be the one currently sending (the
        freeze applies to future dispatch, not an in-flight transfer)."""
        config.dcc_queue = {"dave": [queue_row(user="dave")]}
        config.frozen_queues = {"dave": 1.0}
        config.active_transfers = [{"user": "dave", "file": "Song.flac"}]

        rows = webserver.build_queue_payload()
        self.assertEqual(rows[0]["status"], "sending")

    def test_a_sender_with_no_prior_queue_row_still_appears(self):
        """#220: a free slot with nothing already queued dispatches straight
        into active_transfers and never touches dcc_queue at all (see
        dcc.start_dcc_send()). The summary view used to iterate dcc_queue
        only, so this user was invisible here while /api/stats (reading
        active_transfers directly) correctly showed the transfer."""
        config.dcc_queue = {}
        config.active_transfers = [{"user": "erin", "file": "Live.flac"}]

        rows = {row["user"]: row for row in webserver.build_queue_payload()}

        self.assertIn("erin", rows, "a sending user with no queue row is missing")
        self.assertEqual(rows["erin"]["status"], "sending")
        self.assertEqual(rows["erin"]["preview"], "Live.flac")
        self.assertEqual(rows["erin"]["count"], 0)

    def test_empty_queue_is_an_empty_list(self):
        self.assertEqual(webserver.build_queue_payload(), [])

    def test_user_param_returns_that_users_full_file_list(self):
        config.dcc_queue = {
            "dave": [queue_row(user="dave", filename="One.flac"),
                     queue_row(user="dave", filename="Two.flac")],
        }
        result = webserver.build_queue_payload(user="Dave")
        self.assertEqual(result["user"], "dave")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["files"], ["One.flac", "Two.flac"])

    def test_user_param_for_an_unknown_user_is_empty_not_an_error(self):
        result = webserver.build_queue_payload(user="nobody")
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["files"], [])

    def test_user_param_is_case_and_whitespace_insensitive(self):
        config.dcc_queue = {"dave": [queue_row(user="dave", filename="One.flac")]}
        result = webserver.build_queue_payload(user="  DAVE  ")
        self.assertEqual(result["user"], "dave")
        self.assertEqual(result["count"], 1)


def payload_rows(payload):
    """Flat rows out of a folder-grouped file-list payload.

    Both file-list endpoints page by FOLDER now and return
    {"folders": [{"folder", "count", "entries"}, ...]}. Most tests here are
    asserting things about rows - what a row contains, whether a file is
    present - and do not care how the page was cut, so they read through this
    rather than each learning the grouped shape.

    Tests that are about the PAGING ITSELF use payload["folders"] directly:
    for those, the grouping is the thing under test.
    """
    return [row for group in payload["folders"] for row in group["entries"]]


class SearchAndFilelistsPayloadTests(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.LOCAL_LIST_DIR = self.tree.lists
        config.LIST_BASE_NAME = "DCCore"
        config.NICKNAME = "DCCore"
        config.CHANNEL = "#dccore-test, #dccore-test2"
        write_master_list(self.tree.lists, "DCCore", [
            ("D:\\MUSIC\\Metallica\\Black Album (1991)\\", [
                ("01 - Enter Sandman.flac", "42.31MB"),
                ("02 - Sad But True.flac", "39.02MB"),
                ("00 - Intro.flac", "0.50MB"),
            ]),
            ("D:\\MUSIC\\Metallica\\Reload (1997)\\", [
                ("01 - Fuel.flac", "38.50MB"),
                # Same name+size as one already listed under a different
                # folder - the filelists dedup is expected to collapse this.
                ("00 - Intro.flac", "0.50MB"),
            ]),
        ])

    def test_search_matches_carry_folder_and_size(self):
        rows = webserver.build_search_payload("sandman")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "01 - Enter Sandman.flac")
        self.assertEqual(rows[0]["path"], "D:\\MUSIC\\Metallica\\Black Album (1991)\\")
        self.assertEqual(rows[0]["size"], "42.31MB")

    def test_search_channel_is_the_full_joined_channel_list(self):
        rows = webserver.build_search_payload("fuel")
        self.assertEqual(rows[0]["channel"], "#dccore-test, #dccore-test2")

    def test_search_is_case_insensitive_and_matches_all_words(self):
        self.assertEqual(len(webserver.build_search_payload("SANDMAN")), 1)
        self.assertEqual(len(webserver.build_search_payload("enter sandman")), 1)
        self.assertEqual(len(webserver.build_search_payload("enter fuel")), 0)

    def test_blank_query_returns_no_results(self):
        self.assertEqual(webserver.build_search_payload(""), [])
        self.assertEqual(webserver.build_search_payload("   "), [])
        self.assertEqual(webserver.build_search_payload("---"), [])

    def test_search_respects_its_own_higher_limit_than_irc(self):
        """WEBUI_MAX_SEARCH_RESULTS (50) is not config.MAX_SEARCH_RESULTS (5,
        sized for a channel) - a browser search for a common word must not be
        clipped to five results the way @find deliberately is."""
        write_master_list(self.tree.lists, "DCCore", [
            (None, [(f"Track {i:02d}.flac", "1.00MB") for i in range(10)]),
        ])
        rows = webserver.build_search_payload("track")
        self.assertEqual(len(rows), 10)

    def test_filelists_covers_every_file_and_dedupes_same_name_and_size(self):
        payload = webserver.build_filelists_payload()
        titles = sorted(r["title"] for r in payload_rows(payload))
        # "00 - Intro.flac" is listed under BOTH albums in the fixture, and
        # appears twice now. It used to collapse to one row, because the dedup
        # key was (filename, size) with no folder in it - invisible while rows
        # were a flat list, wrong once they are grouped: the second album
        # would show as missing a track it actually has.
        self.assertEqual(titles, [
            "00 - Intro.flac", "00 - Intro.flac", "01 - Enter Sandman.flac",
            "01 - Fuel.flac", "02 - Sad But True.flac",
        ])
        # `total` counts FOLDERS, the unit of a page. total_files counts rows.
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["total_files"], 5)

    def test_filelists_rows_have_format_and_source(self):
        rows = {r["title"]: r for r in payload_rows(webserver.build_filelists_payload())}
        fuel = rows["01 - Fuel.flac"]
        self.assertEqual(fuel["format"], "FLAC")
        self.assertEqual(fuel["source"], "DCCore")
        self.assertEqual(fuel["size"], "38.50MB")

    def test_execute_search_irc_behaviour_is_unchanged_by_the_refactor(self):
        """The refactor moved execute_search()'s matching loop into
        find_matching_entries() - this proves the IRC-facing result (what gets
        queued to the user, and the total_matches the header reports) did not
        change shape or content."""
        import announce
        headers = []
        real_header = announce.send_search_result_header

        def fake_header(user, term, total, channel):
            headers.append(total)

        announce.send_search_result_header = fake_header
        self.addCleanup(lambda: setattr(announce, "send_search_result_header", real_header))

        list_mod.execute_search(None, "someuser", "sandman", "#dccore-test")

        self.assertEqual(headers, [1])
        self.assertEqual(len(self.oserve.queued), 1)
        sent_user, sent_msg, _is_vip = self.oserve.queued[0]
        self.assertEqual(sent_user, "someuser")
        self.assertIn("01 - Enter Sandman.flac", sent_msg)
        self.assertIn("::INFO:: 42.31MB", sent_msg)

    def test_execute_search_still_matches_nothing_on_an_all_punctuation_term(self):
        """Historical execute_search() edge case, preserved on purpose: a term
        that strips down to zero search words (e.g. "---") has always matched
        nothing, even though find_matching_entries([]) itself now means
        "match everything" for build_filelists_payload()'s benefit."""
        list_mod.execute_search(None, "someuser", "---", "#dccore-test")
        self.assertEqual(self.oserve.queued, [])


class FilelistsPaginationTests(SearchAndFilelistsPayloadTests):
    """Issue #76, option 3: build_filelists_payload()'s offset/limit
    behaviour. Subclasses SearchAndFilelistsPayloadTests to reuse its setUp()
    (the 4-row master list fixture) rather than rebuilding it."""

    def test_default_offset_and_limit_when_omitted(self):
        payload = webserver.build_filelists_payload()
        self.assertEqual(payload["offset"], 0)
        self.assertEqual(payload["limit"], webserver.FILELISTS_DEFAULT_PAGE_SIZE)
        self.assertEqual(payload["total"], 2, "total counts folders, not rows")
        self.assertEqual(payload["returned"], 2)
        self.assertEqual(len(payload["folders"]), 2)  # fixture is smaller than a page

    def test_an_explicit_offset_and_limit_slice_returns_exactly_that_slice(self):
        all_folders = webserver.build_filelists_payload(0, 100)["folders"]
        names_in_order = [g["folder"] for g in all_folders]

        # offset/limit count FOLDERS: page two of one folder each.
        payload = webserver.build_filelists_payload(1, 1)
        self.assertEqual(payload["offset"], 1)
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(payload["total"], 2)
        self.assertEqual([g["folder"] for g in payload["folders"]], names_in_order[1:2])
        # A folder arrives whole or not at all - never split across pages.
        self.assertEqual(len(payload["folders"][0]["entries"]),
                         payload["folders"][0]["count"])

    def test_an_offset_past_the_end_is_an_empty_page_with_the_correct_total(self):
        payload = webserver.build_filelists_payload(999, 50)
        self.assertEqual(payload["folders"], [])
        self.assertEqual(payload["returned"], 0)
        self.assertEqual(payload["total"], 2)

    def test_limit_is_clamped_at_the_documented_ceiling(self):
        payload = webserver.build_filelists_payload(0, webserver.FILELISTS_MAX_PAGE_SIZE + 5000)
        # build_filelists_payload() itself does not clamp - that is
        # parse_pagination_params()'s job, exercised below - but confirms the
        # slice still behaves sanely (returns everything there is) when
        # handed a limit larger than the whole dataset.
        self.assertEqual(len(payload["folders"]), 2)
        self.assertEqual(len(payload_rows(payload)), 5)


class PaginationParamParsingTests(unittest.TestCase):
    """webserver.parse_pagination_params() - the pure (offset, limit)
    validation shared by both GET /api/filelists and
    GET /api/filelists/bot/<nick>. No DCCoreTestCase config isolation needed:
    this touches no config state at all."""

    def test_missing_values_fall_back_to_defaults(self):
        offset, limit = webserver.parse_pagination_params(None, None)
        self.assertEqual(offset, 0)
        self.assertEqual(limit, webserver.FILELISTS_DEFAULT_PAGE_SIZE)

    def test_non_numeric_values_fall_back_to_defaults_not_an_error(self):
        offset, limit = webserver.parse_pagination_params("banana", "banana")
        self.assertEqual(offset, 0)
        self.assertEqual(limit, webserver.FILELISTS_DEFAULT_PAGE_SIZE)

    def test_negative_offset_falls_back_to_zero(self):
        offset, _limit = webserver.parse_pagination_params("-5", "50")
        self.assertEqual(offset, 0)

    def test_negative_or_zero_limit_falls_back_to_the_default(self):
        for bad in ("-5", "0"):
            with self.subTest(limit=bad):
                _offset, limit = webserver.parse_pagination_params("0", bad)
                self.assertEqual(limit, webserver.FILELISTS_DEFAULT_PAGE_SIZE)

    def test_a_valid_explicit_offset_and_limit_pass_through_unchanged(self):
        offset, limit = webserver.parse_pagination_params("40", "75")
        self.assertEqual(offset, 40)
        self.assertEqual(limit, 75)

    def test_limit_is_clamped_at_the_ceiling(self):
        offset, limit = webserver.parse_pagination_params("0", "999999999")
        self.assertEqual(offset, 0)
        self.assertEqual(limit, webserver.FILELISTS_MAX_PAGE_SIZE)

    def test_a_limit_exactly_at_the_ceiling_is_not_reduced(self):
        _offset, limit = webserver.parse_pagination_params("0", str(webserver.FILELISTS_MAX_PAGE_SIZE))
        self.assertEqual(limit, webserver.FILELISTS_MAX_PAGE_SIZE)


class BroadcastSearchTests(DCCoreTestCase):
    """start_broadcast_search()/build_broadcast_status_payload() - the pure
    logic behind POST /api/search/broadcast and its /status counterpart.
    Both routes are login-gated like every other one (see webserver.py's
    module docstring); these pure functions are exercised directly, below
    the Flask route, so this test class is about the mutation/validation
    logic itself, not the login gate."""

    def setUp(self):
        super().setUp()
        self.oserve.irc_connection = "fake-connected-socket"
        config.CHANNEL = "#dccore-test,#dccore-test2"
        config.BROADCAST_SEARCH_CHANNEL = "#dccore-test"

    def test_a_short_term_is_rejected(self):
        status, result = webserver.start_broadcast_search("ab")
        self.assertEqual(status, 400)
        self.assertIn("error", result)
        self.assertFalse(config.broadcast_search_inprogress)

    def test_a_valid_term_starts_listening_and_queues_the_find(self):
        status, result = webserver.start_broadcast_search("sandman")
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "listening")
        self.assertTrue(config.broadcast_search_inprogress)
        self.assertEqual(config.broadcast_search_term, "sandman")
        self.assertEqual(config.broadcast_search_results, [])

        self.assertEqual(len(self.oserve.queued), 1)
        _key, sent_msg, _is_vip = self.oserve.queued[0]
        self.assertEqual(sent_msg, "PRIVMSG #dccore-test :@find sandman\r\n")

    def test_it_never_blocks_the_caller_for_the_window_duration(self):
        real_window = webserver.BROADCAST_SEARCH_WINDOW
        webserver.BROADCAST_SEARCH_WINDOW = 0.2
        self.addCleanup(lambda: setattr(webserver, "BROADCAST_SEARCH_WINDOW", real_window))

        started = time.time()
        webserver.start_broadcast_search("sandman")
        self.assertLess(time.time() - started, 0.1)

    def test_the_window_closes_itself_after_the_duration(self):
        real_window = webserver.BROADCAST_SEARCH_WINDOW
        webserver.BROADCAST_SEARCH_WINDOW = 0.15
        self.addCleanup(lambda: setattr(webserver, "BROADCAST_SEARCH_WINDOW", real_window))

        webserver.start_broadcast_search("sandman")
        self.assertTrue(config.broadcast_search_inprogress)
        time.sleep(0.4)
        self.assertFalse(config.broadcast_search_inprogress)

    def test_a_second_broadcast_while_one_is_open_is_rejected(self):
        webserver.start_broadcast_search("sandman")
        status, result = webserver.start_broadcast_search("another")
        self.assertEqual(status, 409)
        self.assertIn("already in progress", result["error"])

    def test_cooldown_rejects_a_new_broadcast_started_too_soon(self):
        config.last_broadcast_search_at = time.time()
        config.broadcast_search_inprogress = False  # window already closed
        self.set_config(BROADCAST_SEARCH_COOLDOWN=30)
        status, result = webserver.start_broadcast_search("sandman")
        self.assertEqual(status, 429)
        self.assertIn("wait", result["error"])
        self.assertEqual(self.oserve.queued, [])

    def test_cooldown_elapsed_allows_a_new_broadcast(self):
        config.last_broadcast_search_at = time.time() - 999
        config.broadcast_search_inprogress = False
        self.set_config(BROADCAST_SEARCH_COOLDOWN=30)
        status, _result = webserver.start_broadcast_search("sandman")
        self.assertEqual(status, 200)

    def test_no_irc_connection_is_rejected(self):
        self.oserve.irc_connection = None
        status, result = webserver.start_broadcast_search("sandman")
        self.assertEqual(status, 503)
        self.assertIn("error", result)

    def test_a_term_with_embedded_crlf_is_rejected_not_queued(self):
        """BUG 1 regression: raw IRC command injection via CRLF. A `term`
        containing \\r\\n must never reach oserve.queue_message() - the
        outbound line it would build ("PRIVMSG <channel> :@find <term>\\r\\n")
        gets sent byte-for-byte to the live socket by queue_mgr.py, so an
        embedded CRLF would let the caller smuggle arbitrary extra IRC lines
        (QUIT, JOIN, PRIVMSG as this bot, etc.) past the intended one."""
        status, result = webserver.start_broadcast_search(
            "flac\r\nQUIT :pwned-via-broadcast\r\nPRIVMSG #admin :hi")
        self.assertEqual(status, 400)
        self.assertIn("error", result)
        self.assertFalse(config.broadcast_search_inprogress)
        self.assertEqual(self.oserve.queued, [])

    def test_a_term_with_bare_lf_is_also_rejected(self):
        status, result = webserver.start_broadcast_search("flac\nJOIN #secretadmin")
        self.assertEqual(status, 400)
        self.assertEqual(self.oserve.queued, [])

    def test_a_non_string_term_is_rejected_not_silently_coerced(self):
        status, result = webserver.start_broadcast_search({"nested": "dict"})
        self.assertEqual(status, 400)
        self.assertIn("error", result)
        self.assertEqual(self.oserve.queued, [])

    def test_status_payload_reflects_an_open_window(self):
        webserver.start_broadcast_search("sandman")
        config.broadcast_search_results.append({"from": "otherbot", "text": "line one", "received_at": time.time()})
        payload = webserver.build_broadcast_status_payload()
        self.assertTrue(payload["listening"])
        self.assertEqual(payload["term"], "sandman")
        self.assertEqual(len(payload["results"]), 1)

    def test_status_payload_reflects_a_closed_window(self):
        payload = webserver.build_broadcast_status_payload()
        self.assertFalse(payload["listening"])
        self.assertEqual(payload["results"], [])

    def test_expired_deadline_reads_as_not_listening_even_if_flag_is_stale(self):
        config.broadcast_search_inprogress = True
        config.broadcast_search_deadline = time.time() - 5
        payload = webserver.build_broadcast_status_payload()
        self.assertFalse(payload["listening"])

    def test_an_oversized_term_is_rejected_not_queued(self):
        """#162 finding #13: reject_if_unsafe_for_irc_line() checked bytes
        only, never length - a 5000-char search term used to queue a
        5029-byte outbound line with no cap anywhere. Now rejected at the
        boundary, before anything is queued."""
        huge_term = "x" * (webserver.IRC_LINE_FIELD_MAX_LEN + 1)
        status, result = webserver.start_broadcast_search(huge_term)
        self.assertEqual(status, 400)
        self.assertIn("error", result)
        self.assertFalse(config.broadcast_search_inprogress)
        self.assertEqual(self.oserve.queued, [])

    def test_a_term_at_exactly_the_cap_is_not_rejected_by_it(self):
        term = "x" * webserver.IRC_LINE_FIELD_MAX_LEN
        status, _result = webserver.start_broadcast_search(term)
        self.assertEqual(status, 200)

    def test_the_queued_line_never_exceeds_the_real_wire_budget(self):
        """Belt-and-braces half: even a term right at IRC_LINE_FIELD_MAX_LEN
        (300 chars) must still produce a line fit_irc_line() would consider
        safe - proving the emit site does not simply trust the boundary cap
        blindly. Uses non-ASCII so byte length (what actually matters on the
        wire) is exercised, not just character count."""
        import announce
        term = "å" * webserver.IRC_LINE_FIELD_MAX_LEN  # 2 bytes each in UTF-8
        webserver.start_broadcast_search(term)
        self.assertEqual(len(self.oserve.queued), 1)
        _key, sent_msg, _is_vip = self.oserve.queued[0]
        self.assertLessEqual(len(sent_msg.encode("utf-8")), announce.IRC_LINE_BUDGET,
                             "fit_irc_line() measures the full line, \\r\\n included")


class ListUpdateToolTests(DCCoreTestCase):
    """start_list_update()/build_update_list_status_payload() - the pure
    logic behind POST /api/tools/update-list and its /status counterpart.

    Added alongside configure.py's dashboard-first FILE_DIRECTORY question (#170):
    an operator who sets the music directory from the Settings page - the
    place this project's own configure.py now points at, since FILE_DIRECTORY
    is deliberately not in settings_file.REQUIRED - had no way at all to
    then build the master list without a real IRC client or a CLI already
    running.

    threading.Thread is patched to _SyncThread (imported from
    tests.test_commands, which already built it for the identical need in
    ListUpdateTimeoutTests) so commands.handle_list_update_request()'s own
    async_list_updater() runs synchronously and its effect on
    config.update_inprogress is observable with no real background thread
    or real update_list.py subprocess."""

    def setUp(self):
        super().setUp()
        from tests.test_commands import _SyncThread
        self.debug = silence_debug(announce)
        real_thread_cls = threading.Thread
        threading.Thread = _SyncThread
        self.addCleanup(setattr, threading, "Thread", real_thread_cls)
        real_sleep = time.sleep
        time.sleep = lambda *_a, **_k: None
        self.addCleanup(setattr, time, "sleep", real_sleep)

        import subprocess
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            import types
            return types.SimpleNamespace(returncode=0, stdout="List of 1 Files\n", stderr="")

        subprocess.run = fake_run
        self.addCleanup(setattr, subprocess, "run", real_run)

    def test_a_clean_run_starts_and_finishes_synchronously_under_the_fake_thread(self):
        status, result = webserver.start_list_update()
        self.assertEqual(status, 200)
        self.assertEqual(result["update"], "started")
        # _SyncThread ran the whole update inline, so by the time this
        # returns the finally block has already cleared the flag again.
        self.assertFalse(config.update_inprogress)

    def test_a_run_already_in_progress_is_rejected(self):
        config.update_inprogress = True
        status, result = webserver.start_list_update()
        self.assertEqual(status, 409)
        self.assertIn("already running", result["error"])

    def test_a_paused_system_scan_is_rejected_when_pause_on_update_is_set(self):
        config.search_inprogress = True
        config.PAUSE_ON_UPDATE = True
        status, result = webserver.start_list_update()
        self.assertEqual(status, 409)
        self.assertIn("already in progress", result["error"])

    def test_a_system_scan_does_not_block_when_pause_on_update_is_off(self):
        """PAUSE_ON_UPDATE=False means commands.handle_list_update_request()
        itself never even checks search_inprogress - see its own comment -
        so this route must not invent a stricter gate than the command it
        wraps enforces."""
        config.search_inprogress = True
        config.PAUSE_ON_UPDATE = False
        status, _result = webserver.start_list_update()
        self.assertEqual(status, 200)

    def test_status_payload_reflects_a_run_in_progress(self):
        config.update_inprogress = True
        payload = webserver.build_update_list_status_payload()
        self.assertTrue(payload["running"])

    def test_status_payload_reflects_no_run_in_progress(self):
        payload = webserver.build_update_list_status_payload()
        self.assertFalse(payload["running"])

    def test_ok_is_none_before_any_rebuild_has_run(self):
        """Never having run yet is not the same claim as "it failed" -
        web/app.js must not report a failure for a fresh process that has
        simply never rebuilt the list."""
        payload = webserver.build_update_list_status_payload()
        self.assertIsNone(payload["ok"])

    def test_a_successful_rebuild_is_reported_as_ok(self):
        status, _result = webserver.start_list_update()
        self.assertEqual(status, 200)

        payload = webserver.build_update_list_status_payload()
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error"])

    def test_a_failed_rebuild_is_reported_as_not_ok(self):
        """#224: build_update_list_status_payload() used to answer only
        {"running": bool} - nothing recorded whether the rebuild that just
        finished actually succeeded, so web/app.js's poll showed "Done.
        Check Stats for the new file count." the moment `running` flipped
        false, whichever it was. A rebuild that failed - missing
        update_list.py, a bad FILE_DIRECTORY, a mount that timed out - was
        reported to the operator as having worked."""
        import subprocess

        def failing_run(cmd, **kwargs):
            import types
            return types.SimpleNamespace(returncode=1, stdout="", stderr="disk full")

        subprocess.run = failing_run

        status, _result = webserver.start_list_update()
        self.assertEqual(status, 200, "starting the rebuild itself still succeeds")

        payload = webserver.build_update_list_status_payload()
        self.assertFalse(payload["ok"])
        self.assertIsNotNone(payload["error"])
        self.assertIn("disk full", payload["error"])


class FetchRoutesTests(DCCoreTestCase):
    """build_fetch_enqueue_result()/build_fetch_status_payload() - the pure
    logic behind POST /api/fetch/enqueue and GET /api/fetch/status."""

    def test_a_single_object_is_accepted(self):
        status, result = webserver.build_fetch_enqueue_result({"bot": "goodbot", "filename": "Song.flac"})
        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["errors"], [])
        rid = result["created"][0]
        self.assertEqual(config.fetch_queue[rid]["bot"], "goodbot")
        self.assertEqual(config.fetch_queue[rid]["state"], "pending")

    def test_a_list_multi_select_is_accepted(self):
        status, result = webserver.build_fetch_enqueue_result([
            {"bot": "bot1", "filename": "A.flac"},
            {"bot": "bot2", "filename": "B.flac"},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(len(config.fetch_queue), 2)

    def test_missing_fields_are_reported_as_errors_not_silently_dropped(self):
        status, result = webserver.build_fetch_enqueue_result([
            {"bot": "goodbot", "filename": "A.flac"},
            {"bot": "", "filename": "B.flac"},
            {"filename": "C.flac"},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(len(result["errors"]), 2)

    def test_an_empty_or_malformed_payload_shape_is_rejected(self):
        """None/[]/a bare string/a number: the payload itself is the wrong
        SHAPE (not a dict, not a list) - rejected at the top with a single
        "error" key, before any per-item validation runs."""
        for bad in (None, [], "not a dict or list", 42):
            status, result = webserver.build_fetch_enqueue_result(bad)
            self.assertEqual(status, 400, f"payload={bad!r}")
            self.assertIn("error", result)

    def test_an_empty_object_is_shape_valid_but_fails_field_validation(self):
        """{} is a validly-shaped single item - it is rejected the OTHER way,
        as a per-item entry in "errors" (missing bot/filename), not as a
        payload-shape error."""
        status, result = webserver.build_fetch_enqueue_result({})
        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])
        self.assertEqual(len(result["errors"]), 1)

    def test_bot_with_embedded_crlf_is_rejected_not_queued(self):
        """BUG 1 regression: dcc_fetch.check_fetch_queue()'s dispatcher later
        interpolates `bot`/`filename` verbatim into an outbound
        "PRIVMSG <bot> :!<bot> <filename>\\r\\n" line - a CRLF embedded in
        either field would let the caller smuggle extra raw IRC lines
        (QUIT, JOIN #secretadmin, etc.) past the intended one. Reproduces the
        adversarial repro: bot="victimbot\\r\\nQUIT :pwned-by-webhook\\r\\nJOIN #secretadmin"."""
        status, result = webserver.build_fetch_enqueue_result({
            "bot": "victimbot\r\nQUIT :pwned-by-webhook\r\nJOIN #secretadmin",
            "filename": "x.mp3",
        })
        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(config.fetch_queue, {})

    def test_filename_with_embedded_crlf_is_rejected_not_queued(self):
        status, result = webserver.build_fetch_enqueue_result({
            "bot": "goodbot",
            "filename": "song.flac\r\nQUIT :pwned",
        })
        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])
        self.assertEqual(config.fetch_queue, {})

    def test_bot_with_a_ctcp_delimiter_is_rejected_not_queued(self):
        """BUG 1 regression (recurred a THIRD time): \\x01 closes the CTCP
        wrapper dcc_fetch._serve_passive_offer() later builds early, letting
        the receiving peer parse trailing content as a second CTCP or as
        plain text - reject_if_unsafe_for_irc_line() used to reject only
        \\r/\\n, not \\x01, even though dcc_fetch's own equivalent check
        already did. Now both delegate to the same
        dcc_fetch.contains_unsafe_ctcp_bytes()."""
        status, result = webserver.build_fetch_enqueue_result({
            "bot": "evilbot\x01ACTION pwned\x01",
            "filename": "Song.flac",
        })
        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])
        self.assertEqual(config.fetch_queue, {})

    def test_filename_with_a_ctcp_delimiter_is_rejected_not_queued(self):
        status, result = webserver.build_fetch_enqueue_result({
            "bot": "goodbot",
            "filename": "Song\x01.mp3",
        })
        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])
        self.assertEqual(config.fetch_queue, {})

    def test_an_oversized_filename_is_rejected_not_queued(self):
        """#162 finding #13: no route capped filename's length - a 3000-char
        filename used to dispatch a 3032-byte PRIVMSG and leave its own row
        stuck at 'offered' until timeout with no indication why. Now
        rejected at the enqueue boundary."""
        status, result = webserver.build_fetch_enqueue_result({
            "bot": "goodbot",
            "filename": "x" * (webserver.IRC_LINE_FIELD_MAX_LEN + 1),
        })
        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])
        self.assertEqual(config.fetch_queue, {})

    def test_an_oversized_bot_is_rejected_not_queued(self):
        status, result = webserver.build_fetch_enqueue_result({
            "bot": "x" * (webserver.IRC_LINE_FIELD_MAX_LEN + 1),
            "filename": "Song.flac",
        })
        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])
        self.assertEqual(config.fetch_queue, {})

    def test_non_string_bot_and_filename_are_rejected_not_silently_coerced(self):
        """BUG 3 regression: {"bot": {"nested": "dict"}, "filename": 12345}
        used to return 200 and silently str()-coerce into a real queue row.
        Must now be rejected with 400, per-item, and never create a row."""
        status, result = webserver.build_fetch_enqueue_result({
            "bot": {"nested": "dict"},
            "filename": 12345,
        })
        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(config.fetch_queue, {})

    def test_a_bad_item_in_a_multiselect_list_is_rejected_others_still_created(self):
        status, result = webserver.build_fetch_enqueue_result([
            {"bot": "goodbot", "filename": "Good.flac"},
            {"bot": "evilbot\r\nQUIT :pwned", "filename": "Evil.flac"},
            {"bot": 12345, "filename": "Bad.flac"},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(len(config.fetch_queue), 1)

    def test_status_payload_is_ordered_oldest_first_and_carries_the_id(self):
        rid1 = None
        import dcc_fetch
        rid1 = dcc_fetch.enqueue_fetch("bot1", "First.flac")
        config.fetch_queue[rid1]["requested_at"] = 1.0
        rid2 = dcc_fetch.enqueue_fetch("bot2", "Second.flac")
        config.fetch_queue[rid2]["requested_at"] = 2.0

        rows = webserver.build_fetch_status_payload()
        self.assertEqual([r["id"] for r in rows], [rid1, rid2])
        self.assertEqual(rows[0]["bot"], "bot1")

    def test_status_payload_is_empty_list_when_queue_is_empty(self):
        self.assertEqual(webserver.build_fetch_status_payload(), [])


class FetchDeleteResultTests(DCCoreTestCase):
    """build_fetch_delete_result() - the pure logic behind
    POST /api/fetch/<request_id>/delete."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-delete-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

    def _put_row(self, request_id, **overrides):
        row = {
            "bot": "goodbot", "filename": "Song.flac", "request_type": "file",
            "state": "complete", "requested_at": 1.0, "offered_at": 1.0,
            "bytes_received": 10, "total_size": 10, "reason": "",
            "stored_filename": None,
        }
        row.update(overrides)
        config.fetch_queue[request_id] = row
        return row

    def test_unknown_request_id_is_a_404_and_changes_nothing(self):
        status, result = webserver.build_fetch_delete_result("nosuchid")
        self.assertEqual(status, 404)
        self.assertIn("error", result)

    def test_a_completed_row_with_no_file_is_removed_from_the_queue(self):
        self._put_row("r1", state="complete", stored_filename=None)
        status, result = webserver.build_fetch_delete_result("r1")
        self.assertEqual(status, 200)
        self.assertEqual(result["deleted"], "r1")
        self.assertNotIn("r1", config.fetch_queue)

    def test_a_completed_row_deletes_its_file_from_fetched_files_dir(self):
        stored = "r2_Song.flac"
        with open(os.path.join(self.tmp, stored), "w") as f:
            f.write("data")
        self._put_row("r2", state="complete", stored_filename=stored)
        status, result = webserver.build_fetch_delete_result("r2")
        self.assertEqual(status, 200)
        self.assertNotIn("r2", config.fetch_queue)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, stored)))

    def test_a_failed_row_is_removed_even_with_no_file_on_disk(self):
        """dcc_fetch.py's own failure path already removes any partial file -
        a failed row's stored_filename may point at nothing at all, which
        must not turn a delete into a 500."""
        self._put_row("r3", state="failed", stored_filename="never_existed.flac", reason="timeout")
        status, result = webserver.build_fetch_delete_result("r3")
        self.assertEqual(status, 200)
        self.assertNotIn("r3", config.fetch_queue)

    def test_a_rejected_list_archive_is_still_state_complete_and_deletable(self):
        """A rejected list zip (see build_fetch_status_payload()'s own
        comment on list_processing_error) is state == "complete" under the
        hood - the frontend's "Rejected" label is cosmetic, so this must be
        deletable exactly like any other complete row, file included."""
        stored = "r4_list.zip"
        with open(os.path.join(self.tmp, stored), "w") as f:
            f.write("zip bytes")
        self._put_row("r4", state="complete", stored_filename=stored,
                       list_processing_error="zip-slip entry refused")
        status, result = webserver.build_fetch_delete_result("r4")
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, stored)))

    def test_in_flight_states_are_refused_not_deleted(self):
        """"pending" is deliberately NOT in this list - see
        tests/test_fetch_queue_bounds.py, which covers why it is deletable and
        that the other three still are not."""
        for state in ("offered", "listening", "receiving"):
            with self.subTest(state=state):
                rid = f"r-{state}"
                self._put_row(rid, state=state, stored_filename=None)
                status, result = webserver.build_fetch_delete_result(rid)
                self.assertEqual(status, 409)
                self.assertIn(rid, config.fetch_queue)

    def test_the_on_disk_path_is_built_from_the_rows_own_stored_filename_only(self):
        """The request_id itself must never end up in the path being removed -
        only the row's own stored_filename, exactly like api_fetch_download()'s
        identical guarantee. A path-traversal-looking request_id must not
        reach the filesystem at all."""
        stored = "r5_Song.flac"
        with open(os.path.join(self.tmp, stored), "w") as f:
            f.write("data")
        self._put_row("../../../etc/r5", state="complete", stored_filename=stored)
        status, result = webserver.build_fetch_delete_result("../../../etc/r5")
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, stored)))

    def test_a_stored_filename_escaping_fetched_files_dir_is_refused_not_removed(self):
        """#150's review finding: unlike api_fetch_download()
        (Flask's send_from_directory() does its own safe join and raises
        NotFound on anything that escapes `directory`), this route used to
        trust stored_filename with a plain os.path.join() and no re-check of
        its own - fine while _resolve_destination_path() remains the only
        writer of that field, but the delete itself had no independent
        guarantee if that ever stopped being true. Not reachable through the
        normal enqueue path today; this pins the defence-in-depth re-check
        directly, the same way the write path already enforces it."""
        import shutil
        import tempfile
        outside_dir = tempfile.mkdtemp(prefix="dccore-outside-fetched-")
        self.addCleanup(lambda: shutil.rmtree(outside_dir, ignore_errors=True))
        victim = os.path.join(outside_dir, "important.txt")
        with open(victim, "w") as f:
            f.write("do not delete me")
        escaping_stored_filename = os.path.relpath(victim, self.tmp)

        self._put_row("r7", state="complete", stored_filename=escaping_stored_filename)
        status, result = webserver.build_fetch_delete_result("r7")

        self.assertEqual(status, 500)
        self.assertIn("error", result)
        self.assertTrue(os.path.exists(victim), "must never remove a path outside FETCHED_FILES_DIR")

    def test_deleting_a_row_removes_it_from_persisted_history_immediately(self):
        """Not up to 2s later on check_fetch_queue()'s own polling tick - a
        crash in that window would otherwise bring the just-deleted row back
        on the next boot, pointing at a file that no longer exists."""
        import dcc_fetch
        self._put_row("r6", state="complete", stored_filename=None)
        dcc_fetch.check_fetch_queue()  # first tick: persist it
        self.assertIn("r6", db.load_fetch_history())

        status, result = webserver.build_fetch_delete_result("r6")

        self.assertEqual(status, 200)
        self.assertNotIn("r6", db.load_fetch_history())


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed. CI installs "
                                            "requirements-web.txt, so these DO run there; "
                                            "this skip is for a local checkout without it")
class FetchDeleteRouteTests(DCCoreTestCase):
    """POST /api/fetch/<request_id>/delete through the real Flask app."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-delete-route-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(
            WEBUI_TEST_PASSWORD, iterations=1000))
        self.app = webserver.create_app()
        self.client = self.app.test_client()
        log_in_test_client(self.client)

    def test_deleting_an_unknown_id_is_a_404_via_http(self):
        resp = self.client.post("/api/fetch/nosuchid/delete")
        self.assertEqual(resp.status_code, 404)

    def test_deleting_a_completed_fetch_removes_the_row_and_the_file_via_http(self):
        stored = "rid_Song.flac"
        with open(os.path.join(self.tmp, stored), "w") as f:
            f.write("data")
        config.fetch_queue["rid"] = {
            "bot": "goodbot", "filename": "Song.flac", "request_type": "file",
            "state": "complete", "requested_at": 1.0, "offered_at": 1.0,
            "bytes_received": 4, "total_size": 4, "reason": "",
            "stored_filename": stored,
        }
        resp = self.client.post("/api/fetch/rid/delete")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["deleted"], "rid")
        self.assertNotIn("rid", config.fetch_queue)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, stored)))

    def test_deleting_a_pending_fetch_succeeds_via_http(self):
        """Audit #162 finding 27: this used to 409 like the three states
        below, which left a mistaken bulk enqueue clearable only by restarting
        the daemon."""
        config.fetch_queue["rid"] = {
            "bot": "goodbot", "filename": "Song.flac", "request_type": "file",
            "state": "pending", "requested_at": 1.0, "offered_at": None,
            "bytes_received": 0, "total_size": None, "reason": "",
            "stored_filename": None,
        }
        resp = self.client.post("/api/fetch/rid/delete")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("rid", config.fetch_queue)

    def test_deleting_an_in_flight_fetch_is_refused_via_http(self):
        config.fetch_queue["rid"] = {
            "bot": "goodbot", "filename": "Song.flac", "request_type": "file",
            "state": "receiving", "requested_at": 1.0, "offered_at": 1.0,
            "bytes_received": 4, "total_size": 10, "reason": "",
            "stored_filename": None,
        }
        resp = self.client.post("/api/fetch/rid/delete")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("rid", config.fetch_queue)

    def test_the_route_sits_behind_the_login_gate(self):
        self.client.post("/logout")
        resp = self.client.post("/api/fetch/rid/delete")
        self.assertEqual(resp.status_code, 401)


class FetchStatusPayloadConcurrencyTests(DCCoreTestCase):
    """build_fetch_status_payload() used to read config.fetch_queue with no
    lock at all, while dcc_fetch.check_fetch_queue() has always promoted a
    row to 'offered' under dcc_fetch._fetch_lock() as TWO separate
    statements - `row["state"] = "offered"` then `row["offered_at"] = now`
    (dcc_fetch.py, promoting a pending row). Every existing reader of the
    status payload treats state=="offered" and offered_at being set as a
    package - the dashboard shows "waiting on <bot>" only once an offer
    time exists - so a read landing between those two statements used to be
    able to observe "offered" with offered_at still None, a torn pair no
    valid row can actually be in.

    This races a writer performing exactly that two-step promotion, over
    and over, against build_fetch_status_payload(), asserting the pair is
    never torn - then proves the identical writer reliably produces a torn
    read when the read is left unlocked (the pre-fix shape), confirming the
    passing test depends on the fix rather than being compatible with it by
    chance.
    """

    ROUNDS = 5000

    def setUp(self):
        super().setUp()
        import dcc_fetch
        self.rid = dcc_fetch.enqueue_fetch("racebot", "Song.flac")

    def _promote_and_revert(self, stop):
        """The exact two statements dcc_fetch.check_fetch_queue() uses to
        promote a pending row, under the real lock, followed by a revert so
        the row can be promoted again on the next round."""
        import dcc_fetch
        row = config.fetch_queue[self.rid]
        i = 0
        while not stop.is_set() and i < self.ROUNDS:
            with dcc_fetch._fetch_lock():
                row["state"] = "offered"
                row["offered_at"] = time.time()
            with dcc_fetch._fetch_lock():
                row["state"] = "pending"
                row["offered_at"] = None
            i += 1
        stop.set()

    def test_concurrent_promotion_and_status_payload_reads_never_see_a_torn_pair(self):
        stop = threading.Event()
        torn = []

        def reader():
            while not stop.is_set():
                for row in webserver.build_fetch_status_payload():
                    if row["state"] == "offered" and row["offered_at"] is None:
                        torn.append(dict(row))

        writer_thread = threading.Thread(target=self._promote_and_revert, args=(stop,), daemon=True)
        reader_thread = threading.Thread(target=reader, daemon=True)
        writer_thread.start()
        reader_thread.start()
        writer_thread.join(timeout=30)
        stop.set()
        reader_thread.join(timeout=10)

        self.assertFalse(writer_thread.is_alive(), "writer thread never finished - possible deadlock")
        self.assertFalse(reader_thread.is_alive(), "reader thread never finished - possible deadlock")
        self.assertEqual(torn, [],
                         f"observed {len(torn)} torn read(s) of state=='offered' with "
                         f"offered_at still None, despite the shared lock: {torn[:5]}")

    def test_the_torn_pair_is_real_and_visible_without_the_lock(self):
        """Control for the test above: the promotion really does pass through
        a state where `state` is already "offered" and `offered_at` is still
        None, and a reader that does not take the lock really can be looking
        at that instant. Without this, the test above could be passing because
        the torn state does not exist rather than because the lock prevents it.

        Shown by standing at that instant rather than racing to catch it. The
        two statements are the ones check_fetch_queue() runs under the lock;
        between them the row IS torn, and what an unlocked reader would get is
        simply read there.

        The earlier version of this ran two threads for 5000 rounds with
        sys.setswitchinterval() turned down and asserted the race was
        observed. That is a coin toss the interpreter is free to lose - it
        failed on windows-latest / Python 3.10 with "the unlocked control
        workload never observed a torn pair", and passing meant the scheduler
        cooperated, not that the code was right.
        """
        import dcc_fetch
        row = config.fetch_queue[self.rid]

        with dcc_fetch._fetch_lock():
            row["state"] = "offered"
            # Standing between the two statements. An unlocked reader running
            # now sees exactly this - a shallow copy is what
            # build_fetch_status_payload() would have taken.
            unlocked_view = [dict(other) for other in config.fetch_queue.values()]
            row["offered_at"] = time.time()

        torn = [entry for entry in unlocked_view
                if entry["state"] == "offered" and entry["offered_at"] is None]

        self.assertTrue(
            torn,
            "the promotion no longer passes through a torn state, so the "
            "locked test above is not proving anything - if the two writes "
            "have been made atomic some other way, that test should be "
            "rewritten around whatever now guarantees it")

    def test_the_locked_reader_cannot_stand_at_that_instant(self):
        """The other half, and the reason the lock is what fixes it: a reader
        that takes the same lock cannot execute between those two statements
        at all, because the writer is holding it. Asserted by trying, from
        another thread, while the torn state is held."""
        import dcc_fetch
        row = config.fetch_queue[self.rid]
        read_done = threading.Event()
        observed = []

        def locked_reader():
            observed.extend(webserver.build_fetch_status_payload())
            read_done.set()

        with dcc_fetch._fetch_lock():
            row["state"] = "offered"
            reader_thread = threading.Thread(target=locked_reader, daemon=True)
            reader_thread.start()
            # It cannot get in while this block holds the lock. If it could,
            # it would see the torn pair the test above just demonstrated.
            self.assertFalse(read_done.wait(timeout=0.5),
                             "a reader completed a read while the row was torn")
            row["offered_at"] = time.time()

        self.assertTrue(read_done.wait(timeout=10), "the reader never finished")
        reader_thread.join(timeout=10)
        self.assertEqual(
            [entry for entry in observed
             if entry["state"] == "offered" and entry["offered_at"] is None], [],
            "the locked reader still came back with a torn pair")


class ListFetchRoutesTests(DCCoreTestCase):
    """build_list_fetch_enqueue_result()/build_fetched_bot_list_summaries()/
    build_fetched_bot_list_payload() - the pure logic behind
    POST /api/filelists/fetch, GET /api/filelists/bots and
    GET /api/filelists/bot/<nick>."""

    def test_a_clean_bot_nick_is_accepted_and_creates_a_pending_list_row(self):
        status, result = webserver.build_list_fetch_enqueue_result("goodbot")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 1)
        rid = result["created"][0]
        self.assertEqual(config.fetch_queue[rid]["bot"], "goodbot")
        self.assertEqual(config.fetch_queue[rid]["request_type"], "list")
        self.assertEqual(config.fetch_queue[rid]["state"], "pending")
        self.assertEqual(config.fetch_queue[rid]["filename"], "")

    def test_an_empty_or_blank_bot_nick_is_rejected(self):
        for bad in ("", "   "):
            status, result = webserver.build_list_fetch_enqueue_result(bad)
            self.assertEqual(status, 400, f"bot={bad!r}")
            self.assertIn("error", result)
        self.assertEqual(config.fetch_queue, {})

    def test_a_non_string_bot_is_rejected_not_silently_coerced(self):
        status, result = webserver.build_list_fetch_enqueue_result({"nested": "dict"})
        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_bot_with_embedded_crlf_is_rejected_not_queued(self):
        """Same BUG 1 injection class as POST /api/fetch/enqueue's bot/filename
        (see FetchRoutesTests above) - the bot nick typed into the File Lists
        fetch box reaches the exact same outbound-IRC-line boundary
        (check_fetch_queue()'s "PRIVMSG <channel> :@<bot>\\r\\n") and must be
        rejected the same way."""
        status, result = webserver.build_list_fetch_enqueue_result(
            "victimbot\r\nQUIT :pwned-by-webhook\r\nJOIN #secretadmin")
        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_bot_with_a_bare_lf_is_also_rejected(self):
        status, result = webserver.build_list_fetch_enqueue_result("evilbot\nPRIVMSG #x :hi")
        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_bot_with_a_ctcp_delimiter_is_rejected(self):
        """BUG 1 regression, list-fetch route: check_fetch_queue() builds
        this bot's outbound trigger as a bare "PRIVMSG <channel> :@<bot>",
        but a CTCP-wrapped reply is exactly what an offering bot answers
        with, and a \\x01 in `bot` is just as much an injection vector here
        as it is for /api/fetch/enqueue's bot/filename - see
        webserver.reject_if_unsafe_for_irc_line()."""
        status, result = webserver.build_list_fetch_enqueue_result(
            "evilbot\x01ACTION pwned\x01")
        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_summaries_are_empty_when_nothing_has_been_fetched(self):
        self.assertEqual(webserver.build_fetched_bot_list_summaries(), [])

    def test_summaries_reflect_fetched_bots_sorted_by_nick(self):
        # Issue #76, option 2: summaries read the precomputed "entry_count"
        # field, not a stored "entries" list (which no longer exists) - no
        # real on-disk list_path is needed for this one, since
        # build_fetched_bot_list_summaries() never reads the file itself.
        config.fetched_bot_lists = {
            "zbot": {"bot": "zbot", "fetched_at": 111.0, "entry_count": 2},
            "abot": {"bot": "ABot", "fetched_at": 222.0, "entry_count": 0},
        }
        rows = webserver.build_fetched_bot_list_summaries()
        self.assertEqual([r["bot"] for r in rows], ["ABot", "zbot"])
        by_bot = {r["bot"]: r for r in rows}
        self.assertEqual(by_bot["zbot"]["count"], 2)
        self.assertEqual(by_bot["ABot"]["count"], 0)

    def test_bot_payload_returns_404_for_an_unknown_nick(self):
        status, result = webserver.build_fetched_bot_list_payload("nosuchbot")
        self.assertEqual(status, 404)
        self.assertIn("error", result)

    def _make_fetched_entry(self, bot, files, folders=None):
        """A real on-disk list file plus the {"bot","fetched_at","list_path",
        "entry_count","source_zip"} dict process_fetched_list_zip() now
        stores - build_fetched_bot_list_payload() re-parses `list_path` from
        disk on every call (issue #76, option 2), so a fake in-memory
        "entries" list is no longer enough to exercise it."""
        # `folders` when the test is about paging, which counts folders -
        # a flat `files` list is one group and would page as one item.
        path = write_master_list(self.tree.lists, bot,
                                 folders or [(None, files)])
        return {
            "bot": bot,
            "fetched_at": 111.0,
            "list_path": path,
            "entry_count": (len(files) if files is not None
                            else sum(len(f) for _folder, f in folders)),
            "source_zip": "incoming.zip",
        }

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()

    def test_bot_payload_is_case_insensitive_and_matches_own_list_row_shape(self):
        config.fetched_bot_lists = {
            "goodbot": self._make_fetched_entry("GoodBot", [("A.flac", "1.0MB")]),
        }
        status, result = webserver.build_fetched_bot_list_payload("GOODBOT")
        self.assertEqual(status, 200)
        self.assertEqual(result["bot"], "GoodBot")
        rows = [r for g in result["folders"] for r in g["entries"]]
        self.assertEqual(rows[0]["title"], "A.flac")
        # Exactly the row shape build_filelists_payload() uses for our own
        # list - same five keys, nothing more, nothing less. "folder" joined
        # them when the views became folder-grouped.
        self.assertEqual(set(rows[0].keys()),
                         {"title", "size", "format", "source", "folder"})
        self.assertEqual(result["total"], 1, "one folder in this fixture")
        self.assertEqual(result["total_files"], 1)
        self.assertEqual(result["offset"], 0)
        self.assertEqual(result["limit"], webserver.FILELISTS_DEFAULT_PAGE_SIZE)

    def test_bot_payload_paginates_and_reports_the_correct_total(self):
        folders = [(f"D:/MUSIC/Album {i:02d}/", [(f"Track {i:02d}.flac", "1.0MB")])
                   for i in range(10)]
        config.fetched_bot_lists = {
            "pagebot": self._make_fetched_entry("PageBot", None, folders=folders)}

        status, result = webserver.build_fetched_bot_list_payload("pagebot", offset=3, limit=4)
        self.assertEqual(status, 200)
        self.assertEqual(result["total"], 10, "offset/limit count folders")
        self.assertEqual(result["total_files"], 10)
        self.assertEqual(result["offset"], 3)
        self.assertEqual(result["limit"], 4)
        self.assertEqual([g["folder"].rstrip("/") for g in result["folders"]],
                         ["D:/MUSIC/Album 03", "D:/MUSIC/Album 04",
                          "D:/MUSIC/Album 05", "D:/MUSIC/Album 06"])

    def test_bot_payload_offset_past_the_end_is_empty_with_correct_total(self):
        config.fetched_bot_lists = {"onebot": self._make_fetched_entry("OneBot", [("A.flac", "1.0MB")])}

        status, result = webserver.build_fetched_bot_list_payload("onebot", offset=500, limit=50)
        self.assertEqual(status, 200)
        self.assertEqual(result["folders"], [])
        self.assertEqual(result["returned"], 0)
        self.assertEqual(result["total"], 1, "one folder in this fixture")

    def test_bot_payload_reports_a_clear_error_when_the_file_has_gone_missing(self):
        """The extracted file can disappear between the fetch and the view -
        an operator manually clearing data/fetched/, or an unrelated bug.
        This must come back as a clean error response, never a 500 from an
        unhandled exception."""
        entry = self._make_fetched_entry("GoneBot", [("A.flac", "1.0MB")])
        os.remove(entry["list_path"])
        config.fetched_bot_lists = {"gonebot": entry}

        status, result = webserver.build_fetched_bot_list_payload("gonebot")
        self.assertNotEqual(status, 200)
        self.assertIn("error", result)


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed. CI installs "
                                            "requirements-web.txt, so these DO run there; "
                                            "this skip is for a local checkout without it")
class CrlfInjectionHttpRouteTests(DCCoreTestCase):
    """BUG 1 regression, exercised through the real Flask app/test client
    (not just the pure functions above) - end to end, a raw HTTP POST with an
    embedded CRLF in `term`/`bot`/`filename` must come back 400 and must
    never reach oserve.queue_message(). Skipped entirely when Flask is not
    installed, same as the rest of this module's Flask-gated behaviour."""

    def setUp(self):
        super().setUp()
        self.oserve.irc_connection = "fake-connected-socket"
        config.CHANNEL = "#dccore-test,#dccore-test2"
        config.BROADCAST_SEARCH_CHANNEL = "#dccore-test"
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(
            WEBUI_TEST_PASSWORD, iterations=1000))
        self.app = webserver.create_app()
        self.client = self.app.test_client()
        log_in_test_client(self.client)

    def test_broadcast_term_with_crlf_is_rejected_via_http(self):
        resp = self.client.post("/api/search/broadcast", json={
            "term": "flac\r\nQUIT :pwned via broadcast\r\nPRIVMSG #admin :hi",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())
        self.assertEqual(self.oserve.queued, [])
        self.assertFalse(config.broadcast_search_inprogress)

    def test_an_over_long_enqueue_batch_is_a_413_via_http(self):
        """Audit #162 finding 27. 413 is not a status this app returns
        anywhere else, so it is worth confirming Flask ships it rather than
        coercing it - the pure-function coverage is in
        tests/test_fetch_queue_bounds.py."""
        resp = self.client.post("/api/fetch/enqueue", json=[
            {"bot": "goodbot", "filename": f"S{n}.flac"}
            for n in range(webserver.FETCH_ENQUEUE_MAX_ITEMS + 1)])

        self.assertEqual(resp.status_code, 413)
        self.assertIn("error", resp.get_json())
        self.assertEqual(config.fetch_queue, {})

    def test_fetch_enqueue_bot_with_crlf_is_rejected_via_http(self):
        resp = self.client.post("/api/fetch/enqueue", json={
            "bot": "victimbot\r\nQUIT :pwned-by-webhook\r\nJOIN #secretadmin",
            "filename": "x.mp3",
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body["created"], [])
        self.assertEqual(config.fetch_queue, {})

    def test_fetch_enqueue_filename_with_crlf_is_rejected_via_http(self):
        resp = self.client.post("/api/fetch/enqueue", json={
            "bot": "goodbot",
            "filename": "x.mp3\r\nQUIT :pwned",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_fetch_enqueue_non_string_fields_rejected_via_http(self):
        resp = self.client.post("/api/fetch/enqueue", json={
            "bot": {"nested": "dict"},
            "filename": 12345,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_fetch_enqueue_bot_with_ctcp_delimiter_is_rejected_via_http(self):
        """BUG 1 regression, end to end through the real Flask app: a bare
        \\x01 in `bot` used to sail past reject_if_unsafe_for_irc_line()
        (which only checked \\r/\\n) all the way to a 200 response."""
        resp = self.client.post("/api/fetch/enqueue", json={
            "bot": "evilbot2\x01INJECTED\x01",
            "filename": "Song\x01.mp3",
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body["created"], [])
        self.assertEqual(config.fetch_queue, {})

    def test_a_clean_broadcast_still_works_via_http(self):
        """The CRLF/type checks must not false-positive on ordinary input."""
        resp = self.client.post("/api/search/broadcast", json={"term": "sandman"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.oserve.queued), 1)

    def test_a_clean_fetch_enqueue_still_works_via_http(self):
        resp = self.client.post("/api/fetch/enqueue", json={"bot": "goodbot", "filename": "Song.flac"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["created"]), 1)

    def test_filelists_fetch_bot_with_crlf_is_rejected_via_http(self):
        resp = self.client.post("/api/filelists/fetch", json={
            "bot": "victimbot\r\nQUIT :pwned-by-webhook\r\nJOIN #secretadmin",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_filelists_fetch_folder_rar_bot_with_crlf_is_rejected_via_http(self):
        resp = self.client.post("/api/filelists/fetch-folder-rar", json={
            "bot": "victimbot\r\nQUIT :pwned-by-webhook\r\nJOIN #secretadmin",
            "folder": "Artist/Album",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_filelists_fetch_folder_rar_folder_with_crlf_is_rejected_via_http(self):
        """Unlike "list", a "folder" request's `folder` argument has real
        attacker-reachable content that reaches an outbound IRC line - see
        FolderRarFetchRouteTests.test_folder_with_embedded_crlf_is_rejected_not_queued."""
        resp = self.client.post("/api/filelists/fetch-folder-rar", json={
            "bot": "goodbot",
            "folder": "Artist/Album\r\nQUIT :pwned-by-webhook",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_a_clean_filelists_fetch_folder_rar_still_works_via_http(self):
        resp = self.client.post("/api/filelists/fetch-folder-rar", json={
            "bot": "goodbot", "folder": "Artist/Album",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["created"]), 1)

    def test_filelists_fetch_bot_with_ctcp_delimiter_is_rejected_via_http(self):
        resp = self.client.post("/api/filelists/fetch", json={
            "bot": "evilbot\x01ACTION pwned\x01",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_a_clean_filelists_fetch_still_works_via_http(self):
        resp = self.client.post("/api/filelists/fetch", json={"bot": "goodbot"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(len(body["created"]), 1)
        self.assertEqual(config.fetch_queue[body["created"][0]]["request_type"], "list")

    def test_filelists_bots_route_returns_an_empty_list_with_nothing_fetched(self):
        resp = self.client.get("/api/filelists/bots")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_filelists_bot_route_returns_404_for_an_unknown_nick(self):
        resp = self.client.get("/api/filelists/bot/nosuchbot")
        self.assertEqual(resp.status_code, 404)


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed. CI installs "
                                            "requirements-web.txt, so these DO run there; "
                                            "this skip is for a local checkout without it")
class LoginGateTests(DCCoreTestCase):
    """require_login() and the /login, /logout routes it exempts itself from.

    Every route in create_app() sits behind this gate now - these tests are
    the ones that actually exercise logging in and out, rather than assuming
    the fixtures elsewhere (which all log in during setUp) prove it works."""

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(
            WEBUI_TEST_PASSWORD, iterations=1000))
        self.app = webserver.create_app()
        self.client = self.app.test_client()
        # Module-level state, not reset between tests otherwise, and every
        # Flask test client in this process presents the same synthetic
        # remote_addr - so a block one test method leaves behind would reach
        # the next one regardless of run order.
        webserver._web_bad_ips.clear()

    def test_an_unauthenticated_page_request_redirects_to_login(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/login"))

    def test_an_unauthenticated_api_request_gets_json_401_not_a_redirect(self):
        """An API caller (fetch(), not a browser navigation) wants a status
        code and a body it can branch on, not a 302 to an HTML page."""
        resp = self.client.get("/api/queue")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("error", resp.get_json())

    def test_the_login_page_itself_is_reachable_unauthenticated(self):
        """Defect guard: require_login() must exempt exactly this route, or
        an unauthenticated visitor could never reach the form that logs them
        in - the site would refuse everyone, permanently, including its own
        operator."""
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Admin password", resp.data)

    def test_the_right_password_logs_in_and_reaches_protected_routes(self):
        log_in_test_client(self.client, WEBUI_TEST_PASSWORD)

        resp = self.client.get("/api/queue")
        self.assertEqual(resp.status_code, 200)

    def test_the_wrong_password_is_refused_and_leaves_routes_locked(self):
        resp = self.client.post("/login", data={"password": "not-it"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn(b"Incorrect password", resp.data)

        still_locked = self.client.get("/api/queue")
        self.assertEqual(still_locked.status_code, 401)

    def test_repeated_failures_block_the_address_even_with_the_right_password(self):
        """The same password guards the DCC CHAT console (see adminchat.py's
        own _bad_ips), so this form must not be a fourth, unthrottled way to
        guess it. adminchat.MAX_PASSWORD_ATTEMPTS wrong attempts trip the
        block; the very next request is refused even when it finally supplies
        the correct password."""
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.client.post("/login", data={"password": "not-it"})

        resp = self.client.post("/login", data={"password": WEBUI_TEST_PASSWORD})
        self.assertEqual(resp.status_code, 401)
        self.assertIn(b"Too many failed attempts", resp.data)

    def test_the_web_block_pool_is_separate_from_admin_consoles(self):
        """Defect guard: sharing one counter with adminchat.py's DCC console
        would let a web-side attacker spend down the operator's own block
        budget there too - a denial of service against the console, using
        nothing but repeated wrong guesses through this HTTP form."""
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.client.post("/login", data={"password": "not-it"})

        self.assertFalse(adminchat.is_bad_ip("127.0.0.1"),
                         "the DCC console's own block pool must be untouched")

    def test_the_session_cookie_is_samesite_lax(self):
        """One of the mutating routes behind this login sends a real @find
        into a channel. SameSite=Lax is what stops a plain cross-site POST
        from riding in on it - left to the app rather than a browser default."""
        resp = self.client.post("/login", data={"password": WEBUI_TEST_PASSWORD})
        cookie_headers = resp.headers.getlist("Set-Cookie")
        self.assertTrue(any("SameSite=Lax" in h for h in cookie_headers),
                        cookie_headers)

    def test_logout_clears_the_session_and_relocks_every_route(self):
        log_in_test_client(self.client, WEBUI_TEST_PASSWORD)
        self.assertEqual(self.client.get("/api/queue").status_code, 200)

        logout_resp = self.client.post("/logout")
        self.assertEqual(logout_resp.status_code, 302)
        self.assertTrue(logout_resp.headers["Location"].endswith("/login"))

        self.assertEqual(self.client.get("/api/queue").status_code, 401)

    def test_a_static_asset_is_also_behind_the_gate(self):
        """The docstring's claim - "including static assets" - would be false
        if web/style.css slipped past require_login() by being served through
        Flask's separate "static" endpoint rather than a named @app.route."""
        resp = self.client.get("/style.css")
        self.assertEqual(resp.status_code, 302)

        log_in_test_client(self.client, WEBUI_TEST_PASSWORD)
        resp = self.client.get("/style.css")
        self.assertEqual(resp.status_code, 200)


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed. CI installs "
                                            "requirements-web.txt, so these DO run there; "
                                            "this skip is for a local checkout without it")
class FilelistsHttpPaginationTests(DCCoreTestCase):
    """GET /api/filelists and GET /api/filelists/bot/<nick>, end to end
    through the real Flask app - both now return a page object
    ({"entries","total","offset","limit"}), not a bare array, and both honour
    `?offset=&limit=` (issue #76, option 3)."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.LOCAL_LIST_DIR = self.tree.lists
        config.LIST_BASE_NAME = "DCCore"
        config.NICKNAME = "DCCore"
        # Ten folders of one track each. The old fixture put all ten files
        # under no folder at all, which pages as a single group now and would
        # exercise nothing.
        write_master_list(self.tree.lists, "DCCore", [
            (f"D:\\MUSIC\\Album {i:02d}\\", [(f"Track {i:02d}.flac", "1.0MB")])
            for i in range(10)
        ])
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(
            WEBUI_TEST_PASSWORD, iterations=1000))
        self.app = webserver.create_app()
        self.client = self.app.test_client()
        log_in_test_client(self.client)

    def test_own_list_defaults_to_the_documented_page_size(self):
        resp = self.client.get("/api/filelists")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["total"], 10, "ten folders")
        self.assertEqual(body["total_files"], 10)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(body["limit"], webserver.FILELISTS_DEFAULT_PAGE_SIZE)
        self.assertEqual(len(body["folders"]), 10)

    def test_own_list_honours_explicit_offset_and_limit(self):
        resp = self.client.get("/api/filelists?offset=2&limit=3")
        body = resp.get_json()
        self.assertEqual(body["offset"], 2)
        self.assertEqual(body["limit"], 3)
        self.assertEqual(len(body["folders"]), 3, "three FOLDERS, not three rows")

    def test_own_list_invalid_query_values_fall_back_to_defaults_via_http(self):
        resp = self.client.get("/api/filelists?offset=notanumber&limit=-5")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["offset"], 0)
        self.assertEqual(body["limit"], webserver.FILELISTS_DEFAULT_PAGE_SIZE)

    def test_own_list_limit_is_clamped_via_http(self):
        resp = self.client.get("/api/filelists?limit=999999999")
        body = resp.get_json()
        self.assertEqual(body["limit"], webserver.FILELISTS_MAX_PAGE_SIZE)

    def test_fetched_bot_list_is_paginated_via_http(self):
        path = write_master_list(self.tree.lists, "OtherBot", [
            (f"D:/MUSIC/Other {i:02d}/", [(f"Other {i:02d}.flac", "2.0MB")])
            for i in range(7)
        ])
        config.fetched_bot_lists = {
            "otherbot": {"bot": "OtherBot", "fetched_at": 111.0,
                         "list_path": path, "entry_count": 7,
                         "source_zip": "incoming.zip"},
        }
        resp = self.client.get("/api/filelists/bot/otherbot?offset=5&limit=10")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["total"], 7, "seven folders")
        self.assertEqual(body["offset"], 5)
        self.assertEqual(len(body["folders"]), 2, "the tail: folders 5 and 6")


class BroadcastRenderingXssRegressionTests(unittest.TestCase):
    """BUG 2 regression: web/app.js's escapeHtml() (div.textContent = v;
    return div.innerHTML) never encodes `"`. renderBroadcastResults() used to
    string-concatenate it straight into data-bot="..."/data-filename="..."
    HTML attribute values built from entry.bot/entry.filename - which come
    from irc.py's best-effort "!<bot> <filename>" extraction against
    arbitrary text ANY IRC user can PM/NOTICE to the bot during an open
    broadcast window (attacker-controlled, unauthenticated). A filename like
    `innocent.mp3" autofocus onfocus="alert(document.cookie)" x="` broke out
    of the attribute and injected a live event handler.

    The project has no browser/DOM test harness, and none of the rest of the
    stdlib-only suite needs one, so this is a source-level regression check
    rather than a full DOM assertion: it confirms renderBroadcastResults() no
    longer builds those two attributes via string-concatenated innerHTML, and
    uses .dataset assignment (which the browser encodes correctly, with no
    HTML-parsing step for untrusted text to escape through) instead.
    """

    @classmethod
    def setUpClass(cls):
        app_js_path = os.path.join(REPO_ROOT, "web", "app.js")
        with open(app_js_path, "r", encoding="utf-8") as f:
            cls.source = f.read()

    def _extract_function(self, name):
        start = self.source.index(f"function {name}(")
        # A generously-sized window after the signature - good enough to
        # contain this (short) function's whole body without a real JS parser.
        return self.source[start:start + 2500]

    def test_render_broadcast_results_does_not_build_attributes_via_string_concat(self):
        body = self._extract_function("renderBroadcastResults")
        self.assertNotIn('data-bot="', body)
        self.assertNotIn('data-filename="', body)

    def test_render_broadcast_results_uses_dataset_assignment_instead(self):
        body = self._extract_function("renderBroadcastResults")
        self.assertIn(".dataset.bot = entry.bot", body)
        self.assertIn(".dataset.filename = entry.filename", body)

    def test_no_render_function_anywhere_builds_these_attributes_via_string_concat(self):
        """Confirms the audit findings: this exact unsafe pattern - an HTML
        attribute value string-concatenated from a value that can originate
        from IRC (a filename, nick, or search term) - never existed anywhere
        else in this file. Search/Queue/File Lists rendering only ever place
        escapeHtml() output inside text-node content (between <td>...</td>),
        never inside an attribute, so they were never exposed to this bug
        class in the first place."""
        self.assertNotIn('data-bot="', self.source)
        self.assertNotIn('data-filename="', self.source)
        # Extended for the "Get folder as .rar" button (see
        # FolderRarButtonTests below): state.filelistsSource/group.folder are
        # exactly as attacker-controlled as everything else checked above,
        # and must never be string-concatenated into a data-folder="..."
        # attribute either.
        self.assertNotIn('data-folder="', self.source)


class FilelistsDownloadCheckboxTests(unittest.TestCase):
    """The File Lists view's per-file "Download selected" checkboxes -
    row.source/row.title come from another bot's fetched-and-extracted list
    file, exactly as attacker-controlled as entry.bot/entry.filename in
    BroadcastRenderingXssRegressionTests above, and the same BUG 2 shape
    applies. attachFilelistsCheckboxData() exists specifically so
    folderFilesHtml()'s HTML string never carries either value in an
    attribute - it sets both via .dataset once the markup is already in the
    DOM, after the fact."""

    @classmethod
    def setUpClass(cls):
        app_js_path = os.path.join(REPO_ROOT, "web", "app.js")
        with open(app_js_path, "r", encoding="utf-8") as f:
            cls.source = f.read()

    def _extract_function(self, name):
        start = self.source.index(f"function {name}(")
        return self.source[start:start + 2500]

    def test_folder_files_html_does_not_build_the_attributes_via_string_concat(self):
        body = self._extract_function("folderFilesHtml")
        self.assertNotIn('data-bot="', body)
        self.assertNotIn('data-filename="', body)

    def test_attach_checkbox_data_uses_dataset_assignment(self):
        body = self._extract_function("attachFilelistsCheckboxData")
        self.assertIn(".dataset.bot = row.source", body)
        self.assertIn(".dataset.filename = row.title", body)

    def test_the_checkbox_column_is_wired_into_the_render_and_load_paths(self):
        self.assertIn('el.filelistsBody.innerHTML = groups.map(function (group, index)', self.source)
        # attachFilelistsCheckboxData() must run AFTER the innerHTML write it
        # depends on, or it finds no .filelists-check elements yet to attach
        # to - order matters here in a way a plain "is it called somewhere"
        # check would miss.
        render_call = self.source.index('el.filelistsBody.innerHTML = groups.map(function (group, index)')
        # The trailing ";" (rather than a bare substring match) is what tells
        # this apart from "function attachFilelistsCheckboxData(groups) {" -
        # the definition itself, which necessarily sits ABOVE its own call
        # site in the file and would otherwise make this assertion pass or
        # fail for the wrong reason depending on which one .index() found.
        attach_call = self.source.index("attachFilelistsCheckboxData(groups);")
        self.assertGreater(attach_call, render_call,
                           "attachFilelistsCheckboxData() runs before the checkboxes exist")

    def test_download_selected_button_is_wired_up(self):
        self.assertIn(
            'el.filelistsDownloadSelectedBtn.addEventListener("click"', self.source)


class FolderRarButtonTests(unittest.TestCase):
    """The File Lists view's "Get folder as .rar" button - group.folder and
    state.filelistsSource are exactly as attacker-controlled as row.source/
    row.title in FilelistsDownloadCheckboxTests above (another bot's fetched
    list data), so the same BUG 2 shape applies: attachFilelistsFolderRarData()
    exists specifically so folderHeadingHtml()'s HTML string never carries
    either value in an attribute - it sets both via .dataset once the markup
    is already in the DOM, after the fact, mirroring
    attachFilelistsCheckboxData()'s exact pattern."""

    @classmethod
    def setUpClass(cls):
        app_js_path = os.path.join(REPO_ROOT, "web", "app.js")
        with open(app_js_path, "r", encoding="utf-8") as f:
            cls.source = f.read()

    def _extract_function(self, name):
        start = self.source.index(f"function {name}(")
        return self.source[start:start + 2500]

    def test_folder_heading_html_does_not_build_the_attributes_via_string_concat(self):
        body = self._extract_function("folderHeadingHtml")
        self.assertNotIn('data-bot="', body)
        self.assertNotIn('data-folder="', body)

    def test_attach_folder_rar_data_uses_dataset_assignment(self):
        body = self._extract_function("attachFilelistsFolderRarData")
        self.assertIn(".dataset.bot = state.filelistsSource", body)
        self.assertIn(".dataset.folder = group.folder", body)

    def test_attach_folder_rar_data_is_called_alongside_the_checkbox_attach(self):
        """Per the brief: wired in right alongside attachFilelistsCheckboxData(),
        same place, same after-innerHTML timing - not a second, independent
        call site that could drift out of sync with it."""
        checkbox_call = self.source.index("attachFilelistsCheckboxData(groups);")
        rar_call = self.source.index("attachFilelistsFolderRarData(groups);")
        self.assertGreater(rar_call, checkbox_call)
        # No render call, no unrelated code, in between the two - they run
        # back to back.
        between = self.source[
            checkbox_call + len("attachFilelistsCheckboxData(groups);"):rar_call]
        self.assertNotIn("innerHTML", between)

    def test_folder_rar_click_handler_is_delegated_alongside_the_folder_toggle(self):
        """Reuses the existing delegated click listener on el.filelistsBody
        (the one that already handles .folder-toggle) rather than adding a
        second, competing listener."""
        self.assertIn('el.filelistsBody.addEventListener("click"', self.source)
        click_start = self.source.index('el.filelistsBody.addEventListener("click"')
        click_body = self.source[click_start:click_start + 1200]
        self.assertIn(".folder-toggle", click_body)
        self.assertIn(".folder-rar-btn", click_body)

    def test_folder_rar_request_posts_to_the_new_route(self):
        self.assertIn('"/api/filelists/fetch-folder-rar"', self.source)

    def test_folder_rar_button_only_rendered_for_another_bots_list(self):
        """Same gate folderFilesHtml() already applies to the per-file
        checkbox column - packing a folder as .rar only makes sense against
        another bot's list, never our own."""
        body = self._extract_function("folderHeadingHtml")
        self.assertIn('(state.filelistsSource || "__own__") !== "__own__"', body)


class FetchDeleteButtonRegressionTests(unittest.TestCase):
    """The Downloads table's per-row "Delete" button.

    Unlike BUG 2's bot/filename attributes, row.id is never attacker-
    controlled - it is dcc_fetch.enqueue_fetch()'s own uuid4().hex[:12], not
    anything read back from a foreign bot - so string-concatenating it into
    data-request-id="..." does not reopen that bug class. These tests confirm
    that stays true (row.id is the only value concatenated into the button's
    markup) and that the button is actually wired to a confirmation prompt
    and the delete endpoint, not just present in the DOM."""

    @classmethod
    def setUpClass(cls):
        app_js_path = os.path.join(REPO_ROOT, "web", "app.js")
        with open(app_js_path, "r", encoding="utf-8") as f:
            cls.source = f.read()

    def test_delete_button_markup_only_carries_the_servers_own_request_id(self):
        self.assertIn('data-request-id=\\"" +', self.source)
        self.assertNotIn('data-bot="', self.source)
        self.assertNotIn('data-filename="', self.source)
        self.assertNotIn('data-folder="', self.source)

    def test_delete_click_handler_confirms_before_calling_the_delete_route(self):
        start = self.source.index('el.downloadsBody.addEventListener("click"')
        body = self.source[start:start + 1200]
        self.assertIn("window.confirm(", body)
        self.assertIn('"/api/fetch/" + encodeURIComponent(requestId) + "/delete"', body)
        # The confirm() call must gate the request, not just precede it in
        # the source - i.e. still inside the same guard clause/early return.
        self.assertLess(body.index("window.confirm("), body.index("postJson("))


class DownloadTabAndFilelistsSwitcherRegressionTests(unittest.TestCase):
    """New user-supplied text surfaces added alongside the Download tab
    (bot/filename pairs parsed out of the bulk-paste textarea) and the File
    Lists bot-switcher (bot nicks typed into the fetch box, and bot nicks/
    counts returned by GET /api/filelists/bots): same bug class as
    BroadcastRenderingXssRegressionTests above - confirms none of this new
    rendering code builds an HTML attribute via string-concatenated
    innerHTML from any of it, and reuses .textContent/DOM APIs instead.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "web", "app.js"), "r", encoding="utf-8") as f:
            cls.source = f.read()

    def _extract_function(self, name):
        start = self.source.index(f"function {name}(")
        return self.source[start:start + 2500]

    def test_bulk_fetch_messages_are_rendered_via_textcontent(self):
        body = self._extract_function("renderBulkFetchMessages")
        self.assertIn(".textContent = msg.text", body)
        self.assertNotIn("innerHTML +=", body)
        self.assertNotIn("innerHTML +", body)

    def test_filelists_switcher_options_are_built_via_dom_apis(self):
        body = self._extract_function("renderFilelistsSwitcher")
        self.assertIn(".textContent = row.bot", body)
        self.assertNotIn("innerHTML +=", body)
        self.assertNotIn("innerHTML +", body)

    def test_no_new_attribute_built_via_string_concatenated_innerhtml(self):
        """Bot nicks and filenames from the Download tab / File Lists fetch
        box never end up inside an HTML attribute value built by string
        concatenation anywhere in the file - the exact bug class already
        fixed once (BUG 2, see BroadcastRenderingXssRegressionTests above)."""
        self.assertNotIn('data-bot="', self.source)
        self.assertNotIn('data-filename="', self.source)
        self.assertNotIn('data-folder="', self.source)

    def test_bulk_fetch_form_is_wired_up(self):
        self.assertIn('el.bulkFetchForm.addEventListener("submit"', self.source)

    def test_filelists_fetch_form_is_wired_up(self):
        self.assertIn('el.filelistsFetchForm.addEventListener("submit"', self.source)

    def test_bulk_fetch_line_regex_matches_the_shared_bang_bot_filename_pattern(self):
        """Per the brief: reuse the same tolerant "!(\\S+)\\s+(.+)$" pattern
        already used elsewhere, don't reinvent it."""
        self.assertIn("BULK_FETCH_LINE_RE = /^!(\\S+)\\s+(.+)$/", self.source)


class FilelistsPaginationJsRegressionTests(unittest.TestCase):
    """Issue #76, option 3's frontend half: the File Lists table's Prev/Next
    pager. Same "no unsafe innerHTML string-concat with untrusted text"
    discipline as the other rendering regression tests above - the pager's
    own text (a row count/offset, never IRC-originated) is not itself a risk,
    but the page-size constant and wiring are pinned so a future edit can't
    silently drift the frontend page size away from the backend default, or
    drop the reset-to-first-page behaviour."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "web", "app.js"), "r", encoding="utf-8") as f:
            cls.source = f.read()

    def _extract_function(self, name):
        """Unlike the fixed-window extraction the other regression classes in
        this file use (good enough for their short functions), renderFilelistsPager()
        sits directly above the much longer loadFilelists() with no blank-line
        gap, so a fixed window bleeds into the next function's body and its
        (legitimate, elsewhere) innerHTML usage. Balances braces instead, to
        get exactly this one function's body regardless of what follows it."""
        start = self.source.index(f"function {name}(")
        brace_start = self.source.index("{", start)
        depth = 0
        for i in range(brace_start, len(self.source)):
            if self.source[i] == "{":
                depth += 1
            elif self.source[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.source[start:i + 1]
        raise AssertionError(f"unbalanced braces looking for function {name}()")

    def test_page_size_constant_matches_the_documented_backend_default(self):
        """webserver.FILELISTS_DEFAULT_PAGE_SIZE is the source of truth; this
        just pins that the frontend constant was set to the same number."""
        import webserver
        self.assertIn(f"var FILELISTS_PAGE_SIZE = {webserver.FILELISTS_DEFAULT_PAGE_SIZE};", self.source)

    def test_pager_info_is_rendered_via_textcontent_not_innerhtml(self):
        body = self._extract_function("renderFilelistsPager")
        self.assertIn(".textContent =", body)
        self.assertNotIn("innerHTML", body)

    def test_prev_and_next_buttons_are_wired_up(self):
        self.assertIn('el.filelistsPrevBtn.addEventListener("click"', self.source)
        self.assertIn('el.filelistsNextBtn.addEventListener("click"', self.source)

    def test_switching_bot_source_resets_to_the_first_page(self):
        start = self.source.index('el.filelistsSourceSelect.addEventListener("change"')
        body = self.source[start:start + 300]
        self.assertIn("state.filelistsOffset = 0", body)

    def test_load_filelists_requests_offset_and_limit_query_params(self):
        body = self._extract_function("loadFilelists")
        self.assertIn('"?offset=" + offset + "&limit=" + FILELISTS_PAGE_SIZE', body)


class OptionalFlaskDependencyTests(unittest.TestCase):
    """The load-bearing design decision: importing this module, and calling
    start(), must never require Flask to be installed."""

    def test_have_flask_matches_whether_flask_actually_imports(self):
        try:
            import flask  # noqa: F401
            expected = True
        except ImportError:
            expected = False
        self.assertEqual(webserver.HAVE_FLASK, expected)

    def test_start_logs_and_returns_when_flask_is_missing(self):
        real_have_flask = webserver.HAVE_FLASK
        webserver.HAVE_FLASK = False
        self.addCleanup(lambda: setattr(webserver, "HAVE_FLASK", real_have_flask))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            webserver.start()  # must not raise
        self.assertIn("Flask not installed", buffer.getvalue())

    def test_start_logs_and_returns_when_disabled_via_config(self):
        real_have_flask = webserver.HAVE_FLASK
        webserver.HAVE_FLASK = True
        self.addCleanup(lambda: setattr(webserver, "HAVE_FLASK", real_have_flask))
        # Restores absence as absence. Defaulting to True here would have the
        # cleanup CREATE the switch, set to the value this PR exists to stop
        # anything assuming.
        missing = object()
        real_enabled = getattr(config, "WEBUI_ENABLED", missing)
        config.WEBUI_ENABLED = False

        def restore_enabled():
            if real_enabled is missing:
                if hasattr(config, "WEBUI_ENABLED"):
                    del config.WEBUI_ENABLED
            else:
                config.WEBUI_ENABLED = real_enabled

        self.addCleanup(restore_enabled)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            webserver.start()  # must return before ever touching Flask/create_app
        self.assertIn("Disabled via config.WEBUI_ENABLED", buffer.getvalue())


class ListeningStateRenderingTests(unittest.TestCase):
    """Regression: dcc_fetch.py writes a 'listening' state to a fetch-queue
    row (the interim state a passive/reverse DCC SEND offer sits in while we
    wait for the offering bot to connect back), but web/app.js's
    DOWNLOAD_STATE_LABELS and style.css's .status-pill rules were never
    extended for it - the dashboard fell back to the raw string "listening"
    with no matching color for however long that state lasted (up to
    PASSIVE_LISTEN_TIMEOUT, 60s).
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "web", "app.js"), "r", encoding="utf-8") as f:
            cls.app_js = f.read()
        with open(os.path.join(REPO_ROOT, "web", "style.css"), "r", encoding="utf-8") as f:
            cls.style_css = f.read()

    def test_download_state_labels_has_an_entry_for_listening(self):
        start = self.app_js.index("DOWNLOAD_STATE_LABELS = {")
        body = self.app_js[start:start + 300]
        self.assertIn("listening:", body)

    def test_style_css_has_a_rule_for_status_listening(self):
        self.assertIn(".status-pill.status-listening", self.style_css)


class JsonBodyMustBeAnObject(DCCoreTestCase):
    """POST bodies that parse to something other than a JSON object.

    The two routes used `request.get_json(silent=True) or {}`, which only
    substitutes for a FALSY result. A truthy non-dict - `["a"]`, `"text"`, `7`
    - passed straight through to `.get()` on the next line and raised
    AttributeError, which Flask turns into a 500 with a traceback. Every other
    bad input to these routes gets a 400.

    An empty dict is the right substitute rather than an error of its own: it
    is exactly what a missing body already produces, and the validators
    downstream turn that into their normal 400.
    """

    def test_an_object_passes_through_unchanged(self):
        body = {"term": "sandman"}
        self.assertIs(webserver.json_object(body), body)

    def test_truthy_non_objects_become_an_empty_object(self):
        """The bug. Each of these is truthy, so `or {}` left it intact."""
        for bad in (["a", "b"], "text", 7, 1.5, True):
            with self.subTest(body=bad):
                self.assertEqual(webserver.json_object(bad), {})

    def test_falsy_bodies_still_become_an_empty_object(self):
        """Control. `or {}` already handled these - the replacement must too,
        or a request with no body at all starts failing differently."""
        for empty in (None, {}, [], "", 0):
            with self.subTest(body=empty):
                self.assertEqual(webserver.json_object(empty), {})

    def test_the_result_is_always_safe_to_call_get_on(self):
        """The defect stated directly: whatever the peer sent, the next line
        of every route is a .get(), and it must not raise."""
        for bad in (["a"], "text", 7, None, True):
            with self.subTest(body=bad):
                self.assertEqual(webserver.json_object(bad).get("term", ""), "")

    def test_a_non_object_body_reaches_the_normal_400(self):
        """End to end through the pure route logic: a bad body should land on
        the same 400 an empty body gets, not a 500."""
        status, _payload = webserver.start_broadcast_search(
            webserver.json_object(["not", "an", "object"]).get("term", ""))
        self.assertEqual(status, 400)

        status, _payload = webserver.build_list_fetch_enqueue_result(
            webserver.json_object("not an object").get("bot", ""))
        self.assertEqual(status, 400)

    def test_no_post_route_still_coerces_with_or(self):
        """Guards the wiring, not just the helper.

        A route that goes back to `request.get_json(silent=True) or {}`
        reintroduces the 500, and a unit test of json_object() on its own
        would never notice - the helper would still be correct, just unused.
        """
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "webserver.py")
        with io.open(path, encoding="utf-8") as handle:
            source = handle.read()

        # assertFalse, not assertNotIn: the haystack is the whole module, and
        # assertNotIn prints it in full on failure.
        self.assertFalse(
            "get_json(silent=True) or {}" in source,
            "a POST route coerces its body with `or {}` again, which lets a "
            "truthy non-dict through to .get() and returns a 500")


class RejectedListArchiveRenderingTests(DCCoreTestCase):
    """A list archive whose bytes arrived intact but whose contents the
    extraction guard refused.

    dcc_fetch.py deliberately leaves state == "complete" - the transfer
    really did succeed - and records why the archive was refused in
    row["list_processing_error"], explicitly "on the row for the dashboard".
    /api/fetch/status serves it.

    web/app.js never read it. renderDownloads() branched only on state, and
    read row.reason only in the "failed" arm, so a rejected archive - a
    zip-slip attempt by a foreign bot included - rendered as "Complete" with
    a working Download button, indistinguishable from a list that fetched
    perfectly. The field was served and discarded, and the only record of the
    attempt was a single stdout line.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "web", "app.js"), "r", encoding="utf-8") as f:
            cls.app_js = f.read()
        with open(os.path.join(REPO_ROOT, "web", "style.css"), "r", encoding="utf-8") as f:
            cls.style_css = f.read()

    def _render_downloads_source(self):
        """Just renderDownloads()'s body.

        Anchored on a two-space-indented `function` for the end, not a bare
        one: renderDownloads contains an anonymous `function (row)` in its
        rows.map() call, and stopping at that returns six useless lines.
        """
        start = self.app_js.index("  function renderDownloads(")
        end = self.app_js.index(chr(10) + "  function ", start + 10)
        return self.app_js[start:end]

    def test_the_dashboard_reads_the_field_at_all(self):
        """The defect in one line: written three times server side, read zero
        times client side."""
        # assertTrue, not assertIn: the haystack is a whole source file and
        # assertIn prints it in full on failure.
        self.assertTrue(
            "list_processing_error" in self._render_downloads_source(),
            "renderDownloads() still never reads the field the server sets "
            "for it, so a refused archive still renders as a success")

    def test_download_state_labels_has_an_entry_for_rejected(self):
        start = self.app_js.index("DOWNLOAD_STATE_LABELS = {")
        self.assertTrue("rejected:" in self.app_js[start:start + 500],
                        "no display label for a rejected archive")

    def test_style_css_has_a_rule_for_status_rejected(self):
        self.assertTrue(".status-pill.status-rejected" in self.style_css,
                        "the rejected pill has no colour rule, so it renders "
                        "with the default and reads as an ordinary state")

    def test_the_rejected_check_runs_before_the_complete_arm(self):
        """Order matters. A rejected row is ALSO state === "complete", so if
        the complete arm is tested first it gets the Download button anyway
        and the fix does nothing."""
        render = self._render_downloads_source()
        self.assertLess(
            render.index("if (rejected)"),
            # The BRANCH, not a mention of it - the comment above that branch
            # names the same condition and would otherwise match first.
            render.index('else if (state === "complete")'),
            "the complete branch is tested before the rejected check, so a "
            "refused archive still gets a Download button")

    def test_the_server_still_serves_the_field(self):
        """The other half of the contract. A display fix is worthless if the
        field ever stops being sent, and that would break silently."""
        config.fetch_queue = {
            "req1": {"bot": "evilbot", "state": "complete", "requested_at": 1.0,
                     "list_processing_error": "zip entry would extract outside"},
        }
        rows = webserver.build_fetch_status_payload()
        self.assertEqual(rows[0]["list_processing_error"],
                         "zip entry would extract outside")




class FetchRoutesRefuseWhenTheFeatureIsOff(DCCoreTestCase):
    """The two routes that CREATE fetch rows have to honour the same flag
    dcc_fetch.check_fetch_queue() honours.

    oserve.startup() sets config.fetch_feature_disabled when
    FETCHED_FILES_DIR could not be created, and the dispatcher then refuses to
    promote any row past `pending`. Before this, the HTTP routes accepted the
    request anyway: the dashboard reported the fetch queued, and it sat there
    forever with nothing said about why. One [FETCH] line on the console at
    startup was the only evidence.
    """

    def _unset_the_flag(self):
        """Remove the attribute and put it back afterwards. Deleting module
        state in a test that does not restore it leaves the next test to
        discover it, which is the kind of order-dependence that only shows up
        when the suite is run in a different order."""
        had = hasattr(config, "fetch_feature_disabled")
        previous = getattr(config, "fetch_feature_disabled", None)
        if had:
            del config.fetch_feature_disabled

        def restore():
            if had:
                config.fetch_feature_disabled = previous
            elif hasattr(config, "fetch_feature_disabled"):
                del config.fetch_feature_disabled

        self.addCleanup(restore)

    def test_a_file_fetch_is_refused_rather_than_queued(self):
        config.fetch_feature_disabled = True

        status, result = webserver.build_fetch_enqueue_result(
            {"bot": "goodbot", "filename": "Song.flac"})

        self.assertEqual(status, 503)
        self.assertIn("FETCHED_FILES_DIR", result["error"])
        self.assertEqual(config.fetch_queue, {},
                         "the row must not be created - it could never be promoted")

    def test_a_list_fetch_is_refused_rather_than_queued(self):
        config.fetch_feature_disabled = True

        status, result = webserver.build_list_fetch_enqueue_result("goodbot")

        self.assertEqual(status, 503)
        self.assertIn("FETCHED_FILES_DIR", result["error"])
        self.assertEqual(config.fetch_queue, {})

    def test_a_folder_rar_fetch_is_refused_rather_than_queued(self):
        config.fetch_feature_disabled = True

        status, result = webserver.build_folder_rar_fetch_enqueue_result("goodbot", "Artist/Album")

        self.assertEqual(status, 503)
        self.assertIn("FETCHED_FILES_DIR", result["error"])
        self.assertEqual(config.fetch_queue, {})

    def test_a_multi_item_request_creates_none_of_them(self):
        """The bulk shape has its own path through the validator, so it gets
        its own check: partial acceptance would be worse than refusal."""
        config.fetch_feature_disabled = True

        status, _result = webserver.build_fetch_enqueue_result([
            {"bot": "a", "filename": "One.flac"},
            {"bot": "b", "filename": "Two.flac"},
        ])

        self.assertEqual(status, 503)
        self.assertEqual(config.fetch_queue, {})

    def test_the_error_says_what_to_do_about_it(self):
        """An operator reading this in the dashboard has to be able to act on
        it - the cause is a directory that could not be created, and the fix
        needs a restart once that is sorted."""
        config.fetch_feature_disabled = True

        message = webserver.fetch_feature_error()

        self.assertIn("permissions", message.lower())
        self.assertIn("restart", message.lower())

    def test_both_routes_still_work_when_the_feature_is_on(self):
        """Control. reset_config() leaves the flag False, which is the normal
        running state - these must not have been broken by the gate.

        Three different bots, deliberately: "goodbot" for both the "list"
        and "folder" request would now be refused with 409 (they collide -
        see webserver.BOT_ALONE_FETCH_CONFLICT_ERROR and
        dcc_fetch.has_outstanding_bot_alone_request()) - that conflict is
        its own test elsewhere; this one is purely a control that all three
        routes work when the feature is on."""
        config.fetch_feature_disabled = False

        file_status, _f = webserver.build_fetch_enqueue_result(
            {"bot": "goodbot", "filename": "Song.flac"})
        list_status, _l = webserver.build_list_fetch_enqueue_result("listbot")
        folder_status, _r = webserver.build_folder_rar_fetch_enqueue_result("folderbot", "Artist/Album")

        self.assertEqual((file_status, list_status, folder_status), (200, 200, 200))
        self.assertEqual(len(config.fetch_queue), 3)


    def test_the_flag_being_absent_counts_as_off(self):
        """The reason the read is `getattr(..., True)` rather than the
        `getattr(..., False)` it used to be.

        The attribute exists only once oserve.startup() has set it, so reading
        it missing means asking before that point. Accepting a fetch with
        nowhere to put the file is the worse of the two guesses - and #100
        would start the dashboard earlier in the boot sequence than the line
        that sets this, which turns "before that point" from unreachable into
        an ordinary startup window.
        """
        self._unset_the_flag()

        self.assertIsNotNone(webserver.fetch_feature_error(),
                             "a missing flag must read as disabled, not enabled")

        status, _result = webserver.build_fetch_enqueue_result(
            {"bot": "goodbot", "filename": "Song.flac"})

        self.assertEqual(status, 503)
        self.assertEqual(config.fetch_queue, {})

    def test_the_dispatcher_agrees_with_the_routes(self):
        """Both sides of the same flag: the routes refuse to create the row,
        and the dispatcher refuses to promote one that already exists. They
        have to read the missing attribute the same way, or a row created
        under one reading gets stranded by the other."""
        import dcc_fetch

        # Create the row through the real path, with the feature on, so it has
        # exactly the shape the dispatcher expects.
        config.fetch_feature_disabled = False
        request_id = dcc_fetch.enqueue_fetch("somebot", "Song.flac")

        self._unset_the_flag()
        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[request_id]["state"], "pending",
                         "the dispatcher promoted a row while the feature "
                         "reads as disabled")


class FolderRarFetchRouteTests(DCCoreTestCase):
    """build_folder_rar_fetch_enqueue_result() - the pure logic behind
    POST /api/filelists/fetch-folder-rar. Mirrors ListFetchRoutesTests'
    structure for build_list_fetch_enqueue_result(), extended with the
    second attacker-reachable argument ("folder") that "list" fetches never
    had - see the function's own docstring for why both fields are validated
    here, not just "bot"."""

    def test_a_clean_bot_and_folder_are_accepted_and_create_a_pending_folder_row(self):
        status, result = webserver.build_folder_rar_fetch_enqueue_result("goodbot", "Artist/Album (2020)")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 1)
        rid = result["created"][0]
        row = config.fetch_queue[rid]
        self.assertEqual(row["bot"], "goodbot")
        self.assertEqual(row["request_type"], "folder")
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["filename"], "!rar Artist/Album (2020)")
        self.assertEqual(row["requested_filename"], "!rar Artist/Album (2020)")

    def test_an_empty_or_blank_bot_is_rejected(self):
        for bad in ("", "   "):
            status, result = webserver.build_folder_rar_fetch_enqueue_result(bad, "Artist/Album")
            self.assertEqual(status, 400, f"bot={bad!r}")
            self.assertIn("error", result)
        self.assertEqual(config.fetch_queue, {})

    def test_an_empty_or_blank_folder_is_rejected(self):
        for bad in ("", "   "):
            status, result = webserver.build_folder_rar_fetch_enqueue_result("goodbot", bad)
            self.assertEqual(status, 400, f"folder={bad!r}")
            self.assertIn("error", result)
        self.assertEqual(config.fetch_queue, {})

    def test_a_non_string_bot_is_rejected_not_silently_coerced(self):
        status, result = webserver.build_folder_rar_fetch_enqueue_result({"nested": "dict"}, "Artist/Album")
        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_a_non_string_folder_is_rejected_not_silently_coerced(self):
        status, result = webserver.build_folder_rar_fetch_enqueue_result("goodbot", 12345)
        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_bot_with_embedded_crlf_is_rejected_not_queued(self):
        status, result = webserver.build_folder_rar_fetch_enqueue_result(
            "victimbot\r\nQUIT :pwned-by-webhook\r\nJOIN #secretadmin", "Artist/Album")
        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_bot_with_a_ctcp_delimiter_is_rejected(self):
        status, result = webserver.build_folder_rar_fetch_enqueue_result(
            "evilbot\x01ACTION pwned\x01", "Artist/Album")
        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_folder_with_embedded_crlf_is_rejected_not_queued(self):
        """Unlike "list" (no filename argument at all), a "folder" request's
        `folder` argument becomes real content on the outbound wire line
        ("!<bot> !rar <folder>") - an embedded CRLF here is just as much an
        injection vector as bot/filename already are for /api/fetch/enqueue."""
        status, result = webserver.build_folder_rar_fetch_enqueue_result(
            "goodbot", "Artist/Album\r\nQUIT :pwned-by-webhook")
        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_folder_with_a_ctcp_delimiter_is_rejected(self):
        status, result = webserver.build_folder_rar_fetch_enqueue_result(
            "goodbot", "Artist\x01/Album")
        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})


class BotAloneFetchConflictRouteTests(DCCoreTestCase):
    """POST /api/filelists/fetch and POST /api/filelists/fetch-folder-rar
    both refuse a "list"/"folder" request for a bot that already has one of
    either type outstanding - see dcc_fetch.has_outstanding_bot_alone_
    request()'s docstring for why: both request_types match an incoming DCC
    SEND offer on bot alone (neither convention's response filename is
    predictable ahead of time), so letting two of them race for the same bot
    could make a real transfer get silently misattributed to the wrong row.

    dcc_fetch.py's own EnqueueTimeBotAloneCollisionTests covers the
    underlying enqueue_fetch() check in isolation; these tests confirm the
    two HTTP-facing routes surface it as a clean 409 and, critically, still
    create no row at all when refused."""

    def test_folder_request_refused_with_409_when_a_list_request_is_outstanding(self):
        list_status, _l = webserver.build_list_fetch_enqueue_result("goodbot")
        self.assertEqual(list_status, 200)
        before = len(config.fetch_queue)

        status, result = webserver.build_folder_rar_fetch_enqueue_result("goodbot", "Artist/Album")

        self.assertEqual(status, 409)
        self.assertIn("error", result)
        self.assertEqual(len(config.fetch_queue), before)

    def test_list_request_refused_with_409_when_a_folder_request_is_outstanding(self):
        folder_status, _r = webserver.build_folder_rar_fetch_enqueue_result("goodbot", "Artist/Album")
        self.assertEqual(folder_status, 200)
        before = len(config.fetch_queue)

        status, result = webserver.build_list_fetch_enqueue_result("goodbot")

        self.assertEqual(status, 409)
        self.assertIn("error", result)
        self.assertEqual(len(config.fetch_queue), before)

    def test_a_second_list_request_for_the_same_bot_is_also_refused_with_409(self):
        first_status, _ = webserver.build_list_fetch_enqueue_result("goodbot")
        self.assertEqual(first_status, 200)
        before = len(config.fetch_queue)

        status, result = webserver.build_list_fetch_enqueue_result("goodbot")

        self.assertEqual(status, 409)
        self.assertEqual(len(config.fetch_queue), before)

    def test_a_folder_request_for_a_different_bot_still_succeeds(self):
        list_status, _l = webserver.build_list_fetch_enqueue_result("goodbot")
        self.assertEqual(list_status, 200)

        status, result = webserver.build_folder_rar_fetch_enqueue_result("otherbot", "Artist/Album")

        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(len(config.fetch_queue), 2)

    def test_a_new_request_succeeds_once_the_outstanding_one_has_resolved(self):
        list_status, list_result = webserver.build_list_fetch_enqueue_result("goodbot")
        self.assertEqual(list_status, 200)
        list_rid = list_result["created"][0]
        config.fetch_queue[list_rid]["state"] = "complete"

        status, result = webserver.build_folder_rar_fetch_enqueue_result("goodbot", "Artist/Album")

        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(len(config.fetch_queue), 2)

    def test_a_file_fetch_is_never_blocked_by_an_outstanding_list_request(self):
        """'file' rows (POST /api/fetch/enqueue) use exact-match admission
        control and were never ambiguous with 'list'/'folder' - this
        conflict check must not touch that route."""
        list_status, _l = webserver.build_list_fetch_enqueue_result("goodbot")
        self.assertEqual(list_status, 200)

        status, result = webserver.build_fetch_enqueue_result(
            {"bot": "goodbot", "filename": "Song.flac"})

        self.assertEqual(status, 200)
        self.assertEqual(len(config.fetch_queue), 2)


class TheDashboardSwitchFailsClosed(unittest.TestCase):
    """config.py ships `WEBUI_ENABLED = False` and `WEBUI_HOST = "127.0.0.1"`,
    and states why in as many words: a network-facing surface that "should
    never be on just because someone pulled and restarted" (WEBUI_ENABLED),
    put only on loopback unless the operator explicitly widens it
    (WEBUI_HOST) - now on top of the login gate in webserver.py, not instead
    of it.

    Both readers used to fall back to `True` and `"0.0.0.0"` when the name was
    absent. So a missing switch did not just fail to honour the shipped
    default - it put the dashboard on every interface, which is the strongest
    possible inversion of what config.py promises.

    Absent is reachable rather than theoretical: a bare annotation binds no
    name at all, and #100's mandatory-settings work is precisely about
    removing shipped values from settings.
    """

    TEST_PASSWORD_HASH = "pbkdf2_sha256$1000$00$00"  # never actually verified in this class

    def setUp(self):
        self._real_flask = webserver.HAVE_FLASK
        webserver.HAVE_FLASK = True
        self.addCleanup(lambda: setattr(webserver, "HAVE_FLASK", self._real_flask))
        # Replaced for EVERY test in this class, not only the ones that inspect
        # the bind. These drive start() directly, so if the gate under test
        # regresses the real Flask app binds a socket and blocks the whole
        # suite - which is exactly what happened while mutation-testing this
        # change. A test must not be able to start a live listener because the
        # code it is testing broke.
        self.recorded = self._record_run()

    def _unset(self, name):
        """Remove a config attribute for one test and put it back exactly as it
        was - including putting back its absence, if it was absent."""
        missing = object()
        previous = getattr(config, name, missing)
        if previous is not missing:
            delattr(config, name)

        def restore():
            if previous is missing:
                if hasattr(config, name):
                    delattr(config, name)
            else:
                setattr(config, name, previous)

        self.addCleanup(restore)

    def _set(self, name, value):
        self._unset(name)
        setattr(config, name, value)

    def _record_run(self):
        """Stand in for create_app() so start() can be driven all the way to
        the bind without a socket ever being opened."""
        recorded = {}

        class FakeApp:
            def run(self, **kwargs):
                recorded.update(kwargs)

        missing = object()
        previous = getattr(webserver, "create_app", missing)
        webserver.create_app = lambda: FakeApp()

        def restore():
            if previous is missing:
                del webserver.create_app
            else:
                webserver.create_app = previous

        self.addCleanup(restore)
        return recorded

    def test_an_absent_switch_reads_as_disabled(self):
        self._unset("WEBUI_ENABLED")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            webserver.start()

        self.assertIn("Disabled via config.WEBUI_ENABLED", buffer.getvalue(),
                      "a missing switch started an unauthenticated listener")

    def test_an_absent_host_binds_loopback_not_every_interface(self):
        """0.0.0.0 would put the dashboard on the LAN. Loopback is what
        config.py ships, so loopback is what a missing value means."""
        self._set("WEBUI_ENABLED", True)
        self._set("ADMIN_PASSWORD_HASH", self.TEST_PASSWORD_HASH)
        self._unset("WEBUI_HOST")

        with redirect_stdout(io.StringIO()):
            webserver.start()

        self.assertEqual(self.recorded.get("host"), "127.0.0.1")

    def test_an_explicit_host_is_still_honoured(self):
        """Control: failing closed is about the ABSENT case. An operator who
        deliberately binds every interface must still get that."""
        self._set("WEBUI_ENABLED", True)
        self._set("ADMIN_PASSWORD_HASH", self.TEST_PASSWORD_HASH)
        self._set("WEBUI_HOST", "0.0.0.0")

        with redirect_stdout(io.StringIO()):
            webserver.start()

        self.assertEqual(self.recorded.get("host"), "0.0.0.0")

    def test_the_switch_being_on_still_starts_it(self):
        """Control: the gate must not have been welded shut."""
        self._set("WEBUI_ENABLED", True)
        self._set("ADMIN_PASSWORD_HASH", self.TEST_PASSWORD_HASH)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            webserver.start()

        self.assertNotIn("Disabled via", buffer.getvalue())
        self.assertIn("port", self.recorded)

    def test_an_unset_password_hash_refuses_to_start_even_when_enabled(self):
        """The dashboard must never be reachable with no way to log in.
        WEBUI_ENABLED alone is not enough of a gate any more - a password has
        to actually be configured, or start() refuses regardless."""
        self._set("WEBUI_ENABLED", True)
        self._set("ADMIN_PASSWORD_HASH", "")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            webserver.start()

        self.assertIn("ADMIN_PASSWORD_HASH is not set", buffer.getvalue())
        self.assertEqual(self.recorded, {}, "app.run() must not have been reached")


class WebuiFallbacksMatchWhatConfigShips(unittest.TestCase):
    """Every `getattr(config, "WEBUI_*", <default>)` must use the value
    config.py actually ships.

    Derived from the source rather than listed by hand, so a WEBUI_* setting
    added later is covered without anyone remembering to add it here.

    A fallback that disagrees with the shipped default is a second, invisible
    default that applies only when something has already gone wrong - which is
    exactly the moment the permissive answer is the one you least want. These
    four gate a network listener with no authentication on it.
    """

    SOURCES = ("oserve.py", "webserver.py")

    def shipped_defaults(self):
        path = os.path.join(REPO_ROOT, "defaults.py")
        with io.open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        shipped = {}
        for node in tree.body:
            target = value = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target, value = node.target.id, node.value
            elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                  and isinstance(node.targets[0], ast.Name)):
                target, value = node.targets[0].id, node.value
            if target and value is not None:
                try:
                    shipped[target] = ast.literal_eval(value)
                except (ValueError, TypeError, SyntaxError):
                    pass
        return shipped

    def fallbacks(self):
        """[(file, line, name, fallback), ...] for every WEBUI_* getattr."""
        found = []
        for filename in self.SOURCES:
            path = os.path.join(REPO_ROOT, filename)
            with io.open(path, encoding="utf-8") as handle:
                source = handle.read()
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
                    continue
                if len(node.args) != 3:
                    continue
                target, name, default = node.args
                if not (isinstance(target, ast.Name) and target.id == "config"):
                    continue
                if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                    continue
                if not name.value.startswith("WEBUI_"):
                    continue
                try:
                    found.append((filename, node.lineno, name.value,
                                  ast.literal_eval(default)))
                except (ValueError, TypeError, SyntaxError):
                    continue
        return found

    def test_every_webui_fallback_equals_the_shipped_default(self):
        shipped = self.shipped_defaults()

        wrong = []
        for filename, lineno, name, fallback in self.fallbacks():
            if name not in shipped:
                continue
            if shipped[name] != fallback or type(shipped[name]) is not type(fallback):
                wrong.append(f"{filename}:{lineno} {name} ships {shipped[name]!r} "
                             f"but falls back to {fallback!r}")

        self.assertEqual(
            wrong, [],
            "these fallbacks contradict config.py, so a missing setting takes "
            "a different path from the one shipped: " + "; ".join(wrong))

    def test_the_scan_finds_the_call_sites_it_is_meant_to_check(self):
        """Fixture invariant. If the scan stops matching - because the code
        adopts a shape it does not recognise - the test above would pass while
        checking nothing, which is the failure it exists to prevent."""
        found = self.fallbacks()

        self.assertGreaterEqual(
            len(found), 3,
            f"only {len(found)} WEBUI_* fallback(s) found across "
            f"{', '.join(self.SOURCES)}; the scan has probably stopped "
            f"recognising the shape rather than the code having stopped using it")
        self.assertIn("WEBUI_ENABLED", [name for _f, _l, name, _d in found])


class SettingsPayloadTests(DCCoreTestCase):
    """webserver.build_settings_payload()/apply_settings_changes()/
    build_password_change_result() - the pure logic behind
    GET/POST /api/settings and POST /api/settings/password. No Flask
    required; see the module docstring."""

    def setUp(self):
        super().setUp()
        self.settings_path = os.path.join(self.make_tree().root, "settings.conf")
        os.environ["DCCORE_SETTINGS_FILE"] = self.settings_path
        self.addCleanup(os.environ.pop, "DCCORE_SETTINGS_FILE", None)
        # Every successful apply_settings_changes()/build_password_change_result()
        # call below dispatches commands.handle_rehash_request() on its own
        # thread (see apply_settings_changes()'s docstring) - unmocked, that
        # is the REAL production rehash: it reloads config/dcc/announce/
        # security/db/stats_mgr for real, on a thread this test does not wait
        # for, which then races the NEXT test in this process. Defaulting it
        # to a no-op here keeps every test in this class isolated; the two
        # tests that actually need to observe rehash behaviour install their
        # own function on top of this one, and this cleanup (registered
        # first, so it runs LAST) always puts the true original back.
        real_rehash = commands.handle_rehash_request
        commands.handle_rehash_request = lambda *a, **kw: None
        self.addCleanup(setattr, commands, "handle_rehash_request", real_rehash)

    def test_admin_password_hash_never_appears_as_a_field(self):
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash("x", iterations=1000))
        payload = webserver.build_settings_payload()
        names = {f["name"] for c in payload["categories"] for f in c["fields"]}
        self.assertNotIn("ADMIN_PASSWORD_HASH", names)

    def test_admin_password_set_reflects_an_empty_hash(self):
        self.set_config(ADMIN_PASSWORD_HASH="")
        payload = webserver.build_settings_payload()
        self.assertFalse(payload["admin_password_set"])

    def test_admin_password_set_reflects_a_real_hash(self):
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash("x", iterations=1000))
        payload = webserver.build_settings_payload()
        self.assertTrue(payload["admin_password_set"])

    def test_every_declared_overridable_setting_appears_in_exactly_one_category(self):
        """Completeness guard against SETTINGS_CATEGORIES going stale: every
        setting config.py declares and settings_file.is_overridable() accepts
        - other than ADMIN_PASSWORD_HASH, which deliberately never appears as
        a field - must be slotted into exactly one category. A setting that
        fell through would either be silently missing from the page, or land
        twice if a name were copy-pasted into two categories by mistake."""
        types = settings_file.declared_types(vars(config))
        expected = {name for name in types
                    if name != "ADMIN_PASSWORD_HASH"
                    and settings_file.is_overridable(name, getattr(config, name, None))}

        payload = webserver.build_settings_payload()
        seen = [f["name"] for c in payload["categories"] for f in c["fields"]]

        self.assertEqual(sorted(seen), sorted(set(seen)), "a setting appears in more than one category")
        self.assertEqual(set(seen), expected)
        self.assertNotIn("other", [c["id"] for c in payload["categories"]],
                          "a real setting fell through to the catch-all category - "
                          "add it to webserver.SETTINGS_CATEGORIES")

    def test_every_categorised_setting_has_a_human_readable_label(self):
        """Completeness guard against SETTINGS_LABELS going stale, same
        reasoning as the category guard above: every field the page actually
        renders must carry a label that is not just its own raw config.py
        name, or an operator sees MAX_DCC_SLOTS on the form instead of "Max
        simultaneous sends"."""
        payload = webserver.build_settings_payload()
        fields = [f for c in payload["categories"] for f in c["fields"]]

        for field in fields:
            self.assertIn("label", field, f"{field['name']} has no label key at all")
            self.assertNotEqual(field["label"], field["name"],
                               f"{field['name']} has no entry in webserver.SETTINGS_LABELS "
                               f"and is falling back to its raw name")

    def test_a_runtime_only_name_never_appears(self):
        self.set_config(ORIGINAL_NICK="X")
        payload = webserver.build_settings_payload()
        names = {f["name"] for c in payload["categories"] for f in c["fields"]}
        self.assertNotIn("ORIGINAL_NICK", names)

    def test_apply_settings_changes_writes_and_returns_200(self):
        status, result = webserver.apply_settings_changes({"MAX_DCC_SLOTS": "9"})
        self.assertEqual(status, 200)
        self.assertEqual(result["rehash"], "started")
        self.assertIn("MAX_DCC_SLOTS", result["written"])
        with io.open(self.settings_path, encoding="utf-8") as handle:
            entries = settings_file.parse(handle.read())
        self.assertEqual(entries["MAX_DCC_SLOTS"], "9")

    def test_apply_settings_changes_does_not_wait_for_the_rehash_thread(self):
        """Same shape as BroadcastSearchTests.test_it_never_blocks_the_caller_
        for_the_window_duration: commands.handle_rehash_request() must run on
        its own thread, not the request thread, or a save would freeze the
        dashboard for as long as the real rehash (socket I/O, several
        importlib.reload()s) takes."""
        real_rehash = commands.handle_rehash_request
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_rehash(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=10)
            finished.set()

        commands.handle_rehash_request = blocking_rehash
        self.addCleanup(release.set)
        self.addCleanup(setattr, commands, "handle_rehash_request", real_rehash)

        status, _result = webserver.apply_settings_changes({"MAX_DCC_SLOTS": "9"})

        # Nothing has released the rehash yet. If the save had waited for it,
        # the call above could not have returned at all - which is the whole
        # property, stated without measuring anything.
        #
        # This used to assert the call took under 0.1s. That is not the
        # property, it is a proxy for it, and on a loaded CI runner a call
        # that blocks on nothing at all still took 0.316s - so the test failed
        # for a reason it was never about.
        self.assertEqual(status, 200)
        self.assertTrue(entered.wait(timeout=10), "the rehash thread never started")
        self.assertFalse(finished.is_set(),
                         "the calling thread waited for the rehash to finish")

        release.set()
        self.assertTrue(finished.wait(timeout=10), "the dispatched rehash never completed")

    def test_apply_settings_changes_rejects_the_password_hash_directly(self):
        status, result = webserver.apply_settings_changes({"ADMIN_PASSWORD_HASH": "x"})
        self.assertEqual(status, 400)
        self.assertIn("error", result)
        self.assertFalse(os.path.exists(self.settings_path))

    def test_a_mixed_batch_with_the_password_hash_rejects_the_whole_batch(self):
        """apply_settings_changes()'s ADMIN_PASSWORD_HASH check runs before
        any write, on the whole incoming dict - so a batch that smuggles the
        hash in alongside a legitimate setting must be rejected as a whole,
        not have the legitimate half quietly applied. Same shape as
        test_apply_settings_changes_rejects_the_password_hash_directly above,
        but pins the mixed-batch case specifically: that test only ever sent
        a single-key {"ADMIN_PASSWORD_HASH": ...} payload, which could not
        have caught a bug where a legitimate co-occurring key slipped
        through."""
        status, result = webserver.apply_settings_changes(
            {"MAX_DCC_SLOTS": "42", "ADMIN_PASSWORD_HASH": "evilhash"})
        self.assertEqual(status, 400)
        self.assertIn("error", result)
        self.assertFalse(os.path.exists(self.settings_path),
                          "MAX_DCC_SLOTS must not be written when the batch is rejected")

    def test_concurrent_saves_do_not_lose_either_change(self):
        """Two overlapping saves used to race settings_file.save()'s
        read-modify-write cycle: each reads the same starting settings.conf,
        computes text containing only its own change, and whichever atomic
        replace() lands last wins - silently discarding the other caller's
        change even though both callers got a 200. settings_file._save_lock
        now serialises the whole read-modify-write cycle from inside save()
        itself, so this must not happen regardless of timing or which caller
        reaches it.

        A real save() completes fast enough that two threads rarely actually
        overlap inside the danger window on their own, so this widens that
        window by delaying settings_file._atomic_write() itself - the exact
        seam the lost update happens across - long enough that an unlocked
        second caller would reliably start its own read before the first
        caller's write has landed. See the control test below, which uses
        the identical delay with the lock bypassed and shows the loss really
        does happen without it.
        """
        real_atomic_write = settings_file._atomic_write

        def slow_atomic_write(path, text):
            time.sleep(0.2)
            real_atomic_write(path, text)

        settings_file._atomic_write = slow_atomic_write
        self.addCleanup(setattr, settings_file, "_atomic_write", real_atomic_write)

        outcomes = {}

        def call(name, value):
            outcomes[name] = webserver.apply_settings_changes({name: value})

        t1 = threading.Thread(target=call, args=("MAX_DCC_SLOTS", "42"))
        t2 = threading.Thread(target=call, args=("NICKNAME", "RaceNick"))
        t1.start()
        time.sleep(0.05)  # let t1 get into its (locked) save before t2 tries to join in
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertFalse(t1.is_alive(), "first save never finished - possible deadlock")
        self.assertFalse(t2.is_alive(), "second save never finished - possible deadlock")
        self.assertEqual(outcomes["MAX_DCC_SLOTS"][0], 200)
        self.assertEqual(outcomes["NICKNAME"][0], 200)

        with io.open(self.settings_path, encoding="utf-8") as handle:
            entries = settings_file.parse(handle.read())
        self.assertEqual(entries.get("MAX_DCC_SLOTS"), "42",
                          "MAX_DCC_SLOTS was lost to the concurrent save race")
        self.assertEqual(entries.get("NICKNAME"), "RaceNick",
                          "NICKNAME was lost to the concurrent save race")

    def test_without_the_lock_concurrent_saves_can_lose_a_change(self):
        """Control for the test above: with settings_file._save_lock itself
        replaced by a no-op, the identical concurrent workload reliably
        loses one of the two changes - proving the passing test above
        depends on that lock actually being held, rather than being
        incapable of ever catching a regression here. Disabling the lock
        directly (rather than routing around it through some other,
        unlocked entry point) is the only way to write this control now
        that the lock lives inside save() itself: every caller goes through
        the same guarded critical section, by design."""
        real_atomic_write = settings_file._atomic_write
        real_lock = settings_file._save_lock

        def slow_atomic_write(path, text):
            time.sleep(0.2)
            real_atomic_write(path, text)

        settings_file._atomic_write = slow_atomic_write
        settings_file._save_lock = nullcontext()
        self.addCleanup(setattr, settings_file, "_atomic_write", real_atomic_write)
        self.addCleanup(setattr, settings_file, "_save_lock", real_lock)

        def call(name, value):
            settings_file.save(vars(config), {name: value})

        t1 = threading.Thread(target=call, args=("MAX_DCC_SLOTS", "42"))
        t2 = threading.Thread(target=call, args=("NICKNAME", "RaceNick"))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        with io.open(self.settings_path, encoding="utf-8") as handle:
            entries = settings_file.parse(handle.read())
        lost = [name for name, value in (("MAX_DCC_SLOTS", "42"), ("NICKNAME", "RaceNick"))
                if entries.get(name) != value]
        self.assertTrue(lost, "the unlocked control workload never lost a change - this "
                              "test would no longer prove the lock prevents anything; it "
                              "needs a wider delay/window")

    def test_an_unwritable_settings_directory_returns_a_clean_error_not_a_crash(self):
        """settings_file._atomic_write() can raise a plain OSError/
        PermissionError (e.g. the settings directory itself is not writable)
        that is NOT a settings_file.SettingsWriteError.
        _save_settings_and_rehash() must catch that too, or it propagates out
        of the Flask route as an unhandled 500 with a non-JSON body - which
        the frontend's postJson() cannot parse, since it calls res.json()
        unconditionally on the response.

        Faked with a monkeypatch rather than a chmod'd directory: POSIX file
        permission bits are not what actually gates writability on Windows
        (NTFS ACLs are a different mechanism entirely), so a real "make this
        directory read-only" fixture is not portable to the Windows leg of
        CI - this bit exactly that way the first time it was tried. Raising
        directly from _atomic_write() exercises the same except clause
        deterministically on every platform.
        """
        import settings_file
        real_atomic_write = settings_file._atomic_write

        def _raise(*_a, **_k):
            raise OSError("simulated: settings directory is not writable")

        settings_file._atomic_write = _raise
        self.addCleanup(setattr, settings_file, "_atomic_write", real_atomic_write)

        status, result = webserver.apply_settings_changes({"MAX_DCC_SLOTS": "9"})

        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def test_apply_settings_changes_rejects_an_unknown_setting(self):
        status, result = webserver.apply_settings_changes({"NOT_A_REAL_SETTING": "x"})
        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def test_webui_port_is_flagged_as_restart_required(self):
        status, result = webserver.apply_settings_changes({"WEBUI_PORT": "9000"})
        self.assertEqual(status, 200)
        self.assertIn("WEBUI_PORT", result["restart_required"])

    def test_nickname_is_not_flagged_as_restart_required(self):
        status, result = webserver.apply_settings_changes({"NICKNAME": "X"})
        self.assertEqual(status, 200)
        self.assertNotIn("NICKNAME", result["restart_required"])

    def test_password_change_rejects_an_empty_password(self):
        status, result = webserver.build_password_change_result("", "")
        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def test_password_change_rejects_a_mismatched_confirmation(self):
        status, result = webserver.build_password_change_result("newpass1", "newpass2")
        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def test_password_change_applies_after_the_dispatched_rehash_runs(self):
        """settings_file.save() only writes settings.conf - it never touches
        config's own attributes (see its docstring's "THE FILE IS THE
        OPERATOR'S, NOT OURS" section) - so ADMIN_PASSWORD_HASH only actually
        changes once a rehash re-reads the file. See _quiet_reload_config()'s
        docstring for why this patches in a config-only reload rather than
        letting the real commands.handle_rehash_request() run."""
        real_rehash = commands.handle_rehash_request
        done = threading.Event()

        def reload_config_only(*_args, **_kwargs):
            _quiet_reload_config()
            done.set()

        commands.handle_rehash_request = reload_config_only
        self.addCleanup(setattr, commands, "handle_rehash_request", real_rehash)
        self.addCleanup(_quiet_reload_config)

        status, result = webserver.build_password_change_result("newpass1", "newpass1")
        self.assertEqual(status, 200)
        self.assertTrue(done.wait(timeout=2), "the dispatched rehash never ran")
        self.assertTrue(adminchat.verify_password(config.ADMIN_PASSWORD_HASH, "newpass1"))


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed. CI installs "
                                            "requirements-web.txt, so these DO run there; "
                                            "this skip is for a local checkout without it")
class SettingsHttpRouteTests(DCCoreTestCase):
    """GET/POST /api/settings and POST /api/settings/password, end to end
    through the real Flask app - same setUp shape as
    FilelistsHttpPaginationTests."""

    def setUp(self):
        super().setUp()
        self.settings_path = os.path.join(self.make_tree().root, "settings.conf")
        os.environ["DCCORE_SETTINGS_FILE"] = self.settings_path
        self.addCleanup(os.environ.pop, "DCCORE_SETTINGS_FILE", None)
        # See SettingsPayloadTests.setUp()'s comment: unmocked, a successful
        # save dispatches the REAL commands.handle_rehash_request() on its
        # own thread, which this test does not wait for and would otherwise
        # race every test that runs after it in this process.
        real_rehash = commands.handle_rehash_request
        commands.handle_rehash_request = lambda *a, **kw: None
        self.addCleanup(setattr, commands, "handle_rehash_request", real_rehash)
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(
            WEBUI_TEST_PASSWORD, iterations=1000))
        self.app = webserver.create_app()
        self.client = self.app.test_client()
        log_in_test_client(self.client)

    def test_get_settings_returns_the_expected_shape(self):
        resp = self.client.get("/api/settings")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("categories", body)
        self.assertTrue(body["admin_password_set"])
        names = {f["name"] for c in body["categories"] for f in c["fields"]}
        self.assertIn("MAX_DCC_SLOTS", names)
        self.assertNotIn("ADMIN_PASSWORD_HASH", names)

    def test_post_settings_writes_the_real_file_end_to_end(self):
        resp = self.client.post("/api/settings", json={"MAX_DCC_SLOTS": "7"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["rehash"], "started")
        self.assertIn("MAX_DCC_SLOTS", body["written"])
        with io.open(self.settings_path, encoding="utf-8") as handle:
            entries = settings_file.parse(handle.read())
        self.assertEqual(entries["MAX_DCC_SLOTS"], "7")

    def test_post_settings_returns_clean_json_on_an_unwritable_directory(self):
        """End-to-end version of SettingsPayloadTests.test_an_unwritable_
        settings_directory_returns_a_clean_error_not_a_crash: through the
        real Flask route, an unwritable settings directory must come back
        as a JSON 400, not an unhandled 500 whose body isn't JSON at all
        (which is what the frontend's postJson() would choke on).

        Faked with a monkeypatch rather than a chmod'd directory - see the
        pure-logic version of this test for why a real read-only directory
        does not port to Windows CI."""
        import settings_file
        real_atomic_write = settings_file._atomic_write

        def _raise(*_a, **_k):
            raise OSError("simulated: settings directory is not writable")

        settings_file._atomic_write = _raise
        self.addCleanup(setattr, settings_file, "_atomic_write", real_atomic_write)

        resp = self.client.post("/api/settings", json={"MAX_DCC_SLOTS": "9"})

        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIsNotNone(body, "response body was not JSON")
        self.assertIn("error", body)

    def test_post_settings_password_then_logs_in_with_the_new_password(self):
        real_rehash = commands.handle_rehash_request
        done = threading.Event()

        def reload_config_only(*_args, **_kwargs):
            _quiet_reload_config()
            done.set()

        commands.handle_rehash_request = reload_config_only
        self.addCleanup(setattr, commands, "handle_rehash_request", real_rehash)
        self.addCleanup(_quiet_reload_config)

        resp = self.client.post("/api/settings/password", json={
            "new_password": "newpass1", "confirm_password": "newpass1",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(done.wait(timeout=2), "the dispatched rehash never ran")

        webserver._web_bad_ips.clear()
        fresh_client = self.app.test_client()
        login_resp = fresh_client.post("/login", data={"password": "newpass1"})
        self.assertEqual(login_resp.status_code, 302)


if __name__ == "__main__":
    unittest.main()
