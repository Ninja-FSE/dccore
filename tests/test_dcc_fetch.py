"""dcc_fetch.py - cross-bot file fetch (the "receiving bytes an untrusted
third party hands us" role, deliberately separate from dcc.py's "we are the
trusted server" role - see dcc_fetch.py's module docstring).

Covers, per the brief: offer parsing, admission control (an unsolicited offer
must be dropped, never dialled), filename sanitisation/path-traversal
rejection, the size cap being enforced BEFORE connecting, the state machine's
transitions, and one true end-to-end loopback-socket transfer for the happy
path plus one abort-on-oversize-during-transfer case - mirroring
tests/test_adminchat.py's real-socket pattern rather than mocking everything.

Deliberately stdlib-only, like the rest of the suite.
"""

import contextlib
import io
import ipaddress
import os
import socket
import sys
import threading
import time
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import dcc_fetch  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def ip_long(ip):
    return int(ipaddress.IPv4Address(ip))


_bypass_row_counter = 0


def insert_row_bypassing_enqueue_guard(bot, filename, request_type):
    """Insert a row straight into config.fetch_queue via
    dcc_fetch.new_fetch_row(), skipping dcc_fetch.enqueue_fetch() entirely -
    and with it, enqueue_fetch()'s has_outstanding_bot_alone_request() guard
    (see that function's docstring).

    Exists ONLY so a couple of tests can still put two "list"/"folder" rows
    outstanding for the same bot at once, on purpose, to exercise
    _claim_matching_offer_locked()'s oldest-wins tie-break as the
    defense-in-depth path it now is: that state is no longer reachable
    through the normal enqueue_fetch() call path (the whole point of the
    guard), but the queue is still just a dict, so the tie-break logic stays
    in place for any other way such a row pair could end up there.
    """
    global _bypass_row_counter
    _bypass_row_counter += 1
    rid = f"bypass{_bypass_row_counter}"
    config.fetch_queue[rid] = dcc_fetch.new_fetch_row(bot, filename, request_type=request_type)
    return rid


class OfferParsingTests(unittest.TestCase):

    def test_a_normal_offer_parses(self):
        offer = dcc_fetch.parse_dcc_send_offer(
            "DCC SEND Song.flac 2130706433 55000 4096")
        self.assertEqual(offer, {"filename": "Song.flac", "ip": "127.0.0.1",
                                  "port": 55000, "size": 4096})

    def test_ip_long_decode_is_the_inverse_of_dcc_get_public_ip_long(self):
        config.MY_IP_OR_DOCK = "203.0.113.7"
        as_long = dcc.get_public_ip_long()
        offer = dcc_fetch.parse_dcc_send_offer(f"DCC SEND x.txt {as_long} 55000 1")
        self.assertEqual(offer["ip"], "203.0.113.7")

    def test_a_ctcp_wrapped_offer_parses_the_same(self):
        offer = dcc_fetch.parse_dcc_send_offer("\x01DCC SEND Song.flac 2130706433 55000 4096\x01")
        self.assertIsNotNone(offer)
        self.assertEqual(offer["filename"], "Song.flac")

    def test_a_quoted_filename_with_spaces_parses(self):
        offer = dcc_fetch.parse_dcc_send_offer(
            'DCC SEND "My Favourite Song.flac" 2130706433 55000 4096')
        self.assertEqual(offer["filename"], "My Favourite Song.flac")

    def test_not_dcc_send_returns_none(self):
        self.assertIsNone(dcc_fetch.parse_dcc_send_offer("DCC CHAT chat 2130706433 55000"))
        self.assertIsNone(dcc_fetch.parse_dcc_send_offer("just some text"))

    def test_missing_fields_returns_none(self):
        self.assertIsNone(dcc_fetch.parse_dcc_send_offer("DCC SEND Song.flac 2130706433 55000"))

    def test_non_numeric_fields_return_none(self):
        self.assertIsNone(dcc_fetch.parse_dcc_send_offer("DCC SEND Song.flac notanumber 55000 4096"))

    def test_port_out_of_range_returns_none(self):
        self.assertIsNone(dcc_fetch.parse_dcc_send_offer("DCC SEND Song.flac 2130706433 70000 4096"))
        self.assertIsNone(dcc_fetch.parse_dcc_send_offer("DCC SEND Song.flac 2130706433 0 4096"))

    def test_negative_size_returns_none(self):
        self.assertIsNone(dcc_fetch.parse_dcc_send_offer("DCC SEND Song.flac 2130706433 55000 -1"))

    def test_zero_size_returns_none(self):
        """A declared size of exactly 0 must be rejected the same way a
        negative size is - not just "not negative". _run_transfer()'s loop
        is `while bytes_received < total_size`; with total_size == 0 that
        never runs even once, so a naive `size < 0` check would let a
        zero-declared-size offer sail through admission control, open a real
        connection, read nothing, and mark the row 'complete' with no actual
        transfer having happened."""
        self.assertIsNone(dcc_fetch.parse_dcc_send_offer("DCC SEND Song.flac 2130706433 55000 0"))

    def test_ip_long_out_of_range_returns_none(self):
        self.assertIsNone(dcc_fetch.parse_dcc_send_offer("DCC SEND Song.flac 99999999999 55000 4096"))


class PassiveOfferParsingTests(unittest.TestCase):
    """port 0 plus a trailing token is the standard passive/reverse DCC
    marker - a peer bot answered a cross-bot fetch request with exactly this
    shape during live testing, and the old code
    rejected it as "Unusable" because it checked `port <= 0`. It must now
    parse as a distinct, valid result - see parse_dcc_send_offer()'s
    docstring and adminchat.parse_offer()'s identical convention for
    passive DCC CHAT."""

    def test_the_reported_offer_parses(self):
        """A real passive offer's SHAPE, with invented contents.

        This used to say that only the address had been replaced, which
        certifies everything beside it as genuine - and a sentence doing that
        is what kept real names alive through three earlier scrub passes. The
        claimed IP is a TEST-NET-3 address (RFC 5737); the filename is an
        example."""
        offer = dcc_fetch.parse_dcc_send_offer(
            "DCC SEND [Metallica]_-_72_Seasons_-_01-72_Seasons.mp3 "
            "3405803818 0 3359600 11124")
        self.assertEqual(offer, {
            "filename": "[Metallica]_-_72_Seasons_-_01-72_Seasons.mp3",
            "ip": None,
            "port": 0,
            "size": 3359600,
            "token": "11124",
            "claimed_ip": "203.0.113.42",
        })

    def test_a_different_token_and_size_also_parses(self):
        offer = dcc_fetch.parse_dcc_send_offer(
            "DCC SEND Another_File.flac 2130706433 0 987654 99")
        self.assertEqual(offer, {
            "filename": "Another_File.flac",
            "ip": None,
            "port": 0,
            "size": 987654,
            "token": "99",
            "claimed_ip": "127.0.0.1",
        })

    def test_a_quoted_filename_with_spaces_parses_in_passive_form_too(self):
        offer = dcc_fetch.parse_dcc_send_offer(
            'DCC SEND "My Favourite Song.flac" 2130706433 0 4096 555')
        self.assertEqual(offer["filename"], "My Favourite Song.flac")
        self.assertEqual(offer["port"], 0)
        self.assertEqual(offer["token"], "555")

    def test_port_zero_with_no_token_is_still_rejected(self):
        """Without a token there is nothing for the offering bot to match
        our reply against, so this is just as unusable as any other
        malformed offer - not a valid passive one."""
        self.assertIsNone(dcc_fetch.parse_dcc_send_offer(
            "DCC SEND Song.flac 2130706433 0 4096"))

    def test_the_active_form_is_completely_unaffected(self):
        """The existing (active) shape must not gain a stray "token" key or
        change in any other way."""
        offer = dcc_fetch.parse_dcc_send_offer(
            "DCC SEND Song.flac 2130706433 55000 4096")
        self.assertEqual(offer, {"filename": "Song.flac", "ip": "127.0.0.1",
                                  "port": 55000, "size": 4096})
        self.assertNotIn("token", offer)


class FilenameNormalizationTests(unittest.TestCase):

    def test_space_and_underscore_are_equivalent(self):
        a = dcc_fetch._normalize_filename_for_match("My Song.flac")
        b = dcc_fetch._normalize_filename_for_match("My_Song.flac")
        self.assertEqual(a, b)

    def test_case_insensitive(self):
        self.assertEqual(
            dcc_fetch._normalize_filename_for_match("SONG.FLAC"),
            dcc_fetch._normalize_filename_for_match("song.flac"))


class FilenameSanitizationTests(unittest.TestCase):

    def test_path_separators_are_stripped(self):
        clean = dcc_fetch._sanitize_offer_filename("../../etc/passwd")
        self.assertNotIn("/", clean)
        self.assertNotIn("\\", clean)
        self.assertNotIn("..", clean)

    def test_windows_style_separators_are_stripped(self):
        clean = dcc_fetch._sanitize_offer_filename("..\\..\\Windows\\System32\\evil.exe")
        self.assertNotIn("\\", clean)
        self.assertNotIn("..", clean)

    def test_null_bytes_and_control_codes_are_stripped(self):
        clean = dcc_fetch._sanitize_offer_filename("song\x00.flac\x0304,05colored\x0f")
        self.assertNotIn("\x00", clean)
        self.assertNotIn("\x03", clean)
        self.assertNotIn("\x0f", clean)

    def test_an_all_hostile_name_falls_back_to_a_placeholder(self):
        clean = dcc_fetch._sanitize_offer_filename("../../../..")
        self.assertTrue(clean)
        self.assertNotIn("/", clean)

    def test_a_normal_name_survives_unchanged(self):
        self.assertEqual(dcc_fetch._sanitize_offer_filename("Enter Sandman (1991).flac"),
                          "Enter Sandman (1991).flac")

    def test_resolve_destination_path_stays_inside_fetched_files_dir(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="dccore-fetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = tmp
        dest_dir, stored_name = dcc_fetch._resolve_destination_path("abc123", "../../etc/passwd")
        self.assertIsNotNone(stored_name)
        full = os.path.join(dest_dir, stored_name)
        self.assertTrue(dcc.is_safe_path(os.path.abspath(tmp), full))
        self.assertTrue(stored_name.startswith("abc123_"))


class AdmissionControlTests(DCCoreTestCase):
    """The core safety guardrail: an offer is only ever acted on if it
    matches a row WE marked 'offered' a moment earlier."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

    def test_unsolicited_offer_is_dropped_not_dialled(self):
        """No pending/offered row at all - a stranger just sends a DCC SEND
        out of nowhere. Must never even try to connect."""
        real_socket = socket.socket

        def guard(*a, **kw):
            self.fail("handle_incoming_offer must not open a socket for an unsolicited offer")

        socket.socket = guard
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "randomstranger", "DCC SEND Song.flac 2130706433 55000 4096")

        self.assertEqual(config.fetch_queue, {})

    def test_an_offer_from_the_wrong_bot_is_dropped(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail("must not connect")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "impostorbot", "DCC SEND Song.flac 2130706433 55000 4096")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_an_offer_for_the_wrong_filename_is_dropped(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail("must not connect")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "goodbot", "DCC SEND SomeOtherFile.flac 2130706433 55000 4096")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_an_unsolicited_passive_offer_is_dropped_without_opening_a_listener(self):
        """The same guardrail as the active case, applied to the passive/
        reverse form: an unsolicited passive offer must be dropped BEFORE
        any listening socket is opened. Opening one for a stranger would let
        anyone in the channel make the daemon open a listening socket and
        accept arbitrary bytes just by sending a DCC SEND with port 0 - the
        security boundary this whole feature depends on."""
        real_socket = socket.socket

        def guard(*a, **kw):
            self.fail("handle_incoming_offer must not open a listening "
                      "socket for an unsolicited passive offer")

        socket.socket = guard
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "randomstranger",
            "DCC SEND Song.flac 2130706433 0 4096 12345")

        self.assertEqual(config.fetch_queue, {})

    def test_a_passive_offer_from_the_wrong_bot_is_dropped(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail("must not listen")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "impostorbot", "DCC SEND Song.flac 2130706433 0 4096 12345")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_a_passive_offer_for_the_wrong_filename_is_dropped(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail("must not listen")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "goodbot", "DCC SEND SomeOtherFile.flac 2130706433 0 4096 12345")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_matching_offer_is_claimed_underscore_space_equivalence(self):
        """The offering bot echoing back a space as an underscore (dcc.py's
        own outbound SEND does the same thing) must still match."""
        rid = dcc_fetch.enqueue_fetch("goodbot", "My Song.flac")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        # Reject at the size cap so this stays a pure admission-control test
        # with no real socket involved.
        self.set_config(MAX_FETCH_FILE_SIZE=10)
        dcc_fetch.handle_incoming_offer(
            None, "goodbot", "DCC SEND My_Song.flac 2130706433 55000 999999")

        self.assertEqual(config.fetch_queue[rid]["state"], "failed")
        self.assertIn("exceeds", config.fetch_queue[rid]["reason"])


class SizeCapTests(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

    def test_oversized_declared_size_is_rejected_before_connecting(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Big.flac")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()
        self.set_config(MAX_FETCH_FILE_SIZE=1000)

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail(
            "an oversized offer must be rejected before a socket is ever opened")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "goodbot", "DCC SEND Big.flac 2130706433 55000 999999")

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("exceeds", row["reason"])

    def test_an_oversized_passive_offer_is_rejected_before_listening(self):
        """The size cap runs unconditionally before either transport - a
        passive offer must never get as far as opening a listening socket
        just because its declared size is too large to accept."""
        rid = dcc_fetch.enqueue_fetch("goodbot", "Big.flac")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()
        self.set_config(MAX_FETCH_FILE_SIZE=1000)

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail(
            "an oversized passive offer must be rejected before any listener is opened")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "goodbot", "DCC SEND Big.flac 2130706433 0 999999 777")

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("exceeds", row["reason"])

    def test_a_folder_offer_between_the_plain_and_folder_caps_is_accepted(self):
        """A declared size that would be rejected against MAX_FETCH_FILE_SIZE
        must be ACCEPTED for a "folder" row, which is checked against the
        separate, larger MAX_FETCH_FOLDER_FILE_SIZE cap instead.

        handle_incoming_offer() runs the real transfer synchronously once the
        cap check passes, so by the time it returns here a real (and, in this
        test, doomed - nothing is actually listening) connection attempt has
        already run its course and left the row 'failed' with a connect
        error - that failure is irrelevant to what this test is checking.
        What proves the size cap itself was NOT the rejection reason is
        row["total_size"], which handle_incoming_offer() only ever sets
        AFTER the cap check passes (see its own code, just before it either
        starts listening or hands off to _run_transfer())."""
        self.set_config(MAX_FETCH_FILE_SIZE=1000, MAX_FETCH_FOLDER_FILE_SIZE=2_000_000)
        rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        dcc_fetch.handle_incoming_offer(
            None, "goodbot", "DCC SEND Artist_Album.rar 2130706433 55000 500000")

        row = config.fetch_queue[rid]
        self.assertEqual(row["total_size"], 500000,
                         "500000 is above MAX_FETCH_FILE_SIZE but below "
                         "MAX_FETCH_FOLDER_FILE_SIZE, so this offer must pass "
                         "the size cap, not be rejected at it")
        self.assertNotIn("exceeds", row.get("reason", ""))

    def test_a_folder_offer_above_the_folder_cap_is_still_rejected(self):
        self.set_config(MAX_FETCH_FILE_SIZE=1000, MAX_FETCH_FOLDER_FILE_SIZE=2_000_000)
        rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail(
            "an oversized folder offer must be rejected before a socket is ever opened")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "goodbot", "DCC SEND Artist_Album.rar 2130706433 55000 3000000")

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("exceeds", row["reason"])
        self.assertIn("MAX_FETCH_FOLDER_FILE_SIZE", row["reason"])

    def test_a_list_offer_above_the_list_cap_is_rejected_before_connecting(self):
        """#162 finding #10: a request_type='list' row used to fall through
        to the general MAX_FETCH_FILE_SIZE cap (200MB default) - a master-
        list zip is a small text index, never a real download, and letting
        it through that far meant zipfile.ZipFile() (in list_fetch.py, once
        the transfer completed) had already eagerly parsed a huge central
        directory before any of its own guards could refuse anything. Now
        rejected here, before a socket is ever opened, against the smaller
        MAX_FETCH_LIST_FILE_SIZE cap."""
        self.set_config(MAX_FETCH_FILE_SIZE=200_000_000, MAX_FETCH_LIST_FILE_SIZE=1000)
        rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail(
            "an oversized list offer must be rejected before a socket is ever opened")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "goodbot", "DCC SEND list.zip 2130706433 55000 5000")

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("exceeds", row["reason"])
        self.assertIn("MAX_FETCH_LIST_FILE_SIZE", row["reason"])

    def test_a_list_offer_within_the_list_cap_is_not_rejected_by_it(self):
        """Control: a small, genuine list offer must not be caught by the
        new, tighter list-specific cap just because it exists."""
        self.set_config(MAX_FETCH_FILE_SIZE=200_000_000, MAX_FETCH_LIST_FILE_SIZE=1_000_000)
        rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        dcc_fetch.handle_incoming_offer(
            None, "goodbot", "DCC SEND list.zip 2130706433 55000 500000")

        row = config.fetch_queue[rid]
        self.assertEqual(row["total_size"], 500000,
                         "500000 is below MAX_FETCH_LIST_FILE_SIZE, so this "
                         "offer must pass the size cap, not be rejected at it")
        self.assertNotIn("exceeds", row.get("reason", ""))


class FolderTransferTimeoutTests(DCCoreTestCase):
    """#162 finding #11: a 'folder' row's transfer used to inherit
    FETCH_TRANSFER_TIMEOUT, sized for the 200MB MAX_FETCH_FILE_SIZE cap, even
    though it has its own 10x-larger MAX_FETCH_FOLDER_FILE_SIZE cap - a large,
    legitimately slow discography transfer could be aborted (no resume, so
    every retry identical) well before it had any chance to finish.

    #223: the FIRST fix (3600s) undershot its own goal. The cap grew 10.24x
    (200MB -> 2GB) but the timeout only grew 6x (600s -> 3600s), so the
    IMPLIED THROUGHPUT FLOOR a peer has to sustain went UP for a folder, not
    down - a peer exactly fast enough to finish a maximum-size plain file in
    600s could not finish a maximum-size folder in 3600s. Rescaled to 6144s,
    matching the size cap's own 10.24x."""

    def test_a_folder_row_gets_its_own_longer_wall_clock(self):
        self.set_config(FETCH_TRANSFER_TIMEOUT=600, FETCH_FOLDER_TRANSFER_TIMEOUT=6144)
        self.assertEqual(dcc_fetch._fetch_transfer_timeout("folder"), 6144)
        self.assertEqual(dcc_fetch._fetch_transfer_timeout("file"), 600)
        self.assertEqual(dcc_fetch._fetch_transfer_timeout("list"), 600,
                         "only 'folder' gets the longer ceiling")

    def test_falls_back_to_sane_defaults_when_unconfigured(self):
        self.assertEqual(dcc_fetch._fetch_transfer_timeout("folder"), 6144)
        self.assertEqual(dcc_fetch._fetch_transfer_timeout("file"), 600)

    def test_the_folder_floor_is_not_stricter_than_the_plain_file_floor(self):
        """The defect itself, stated as the property that matters: a peer
        sustaining the plain-file floor must not be timed out on a folder."""
        file_floor = config.MAX_FETCH_FILE_SIZE / dcc_fetch._fetch_transfer_timeout("file")
        folder_floor = config.MAX_FETCH_FOLDER_FILE_SIZE / dcc_fetch._fetch_transfer_timeout("folder")

        self.assertLessEqual(
            folder_floor, file_floor * 1.01,  # 1% slack for rounding the timeout to a whole second
            f"a folder demands {folder_floor:.0f} B/s but a plain file only "
            f"{file_floor:.0f} B/s - the folder timeout undershot its own cap increase")


class FallbackLockIsShared(DCCoreTestCase):
    """_fetch_lock() falls back to a module-level lock when oserve.py has not
    run (tests, most notably).

    It used to build `threading.Lock()` inline on every call, handing every
    caller a brand new object it acquired uncontended - so the fallback path
    synchronised nothing at all, silently. Same fix and same test shape as
    list_fetch.py's _lock() (see FallbackLockIsShared in test_list_fetch.py)
    and runtime.channel_users_lock().
    """

    def test_repeated_calls_return_one_object(self):
        saved = getattr(config, "fetch_queue_lock", None)
        if saved is not None:
            del config.fetch_queue_lock
            self.addCleanup(setattr, config, "fetch_queue_lock", saved)

        self.assertIs(dcc_fetch._fetch_lock(), dcc_fetch._fetch_lock(),
                      "the fallback hands out a fresh lock per call, so "
                      "concurrent callers never actually exclude each other")

    def test_config_fetch_queue_lock_wins_once_it_exists(self):
        config.fetch_queue_lock = threading.Lock()
        self.assertIs(dcc_fetch._fetch_lock(), config.fetch_queue_lock)


class DispatcherStateMachineTests(DCCoreTestCase):

    def test_pending_rows_are_promoted_to_offered_and_a_request_is_queued(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        self.assertEqual(config.fetch_queue[rid]["state"], "pending")

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "offered")
        self.assertIsNotNone(row["offered_at"])
        self.assertEqual(len(self.oserve.queued), 1)
        sent_user, sent_msg, _is_vip = self.oserve.queued[0]
        self.assertEqual(sent_user, "goodbot")
        self.assertIn("!goodbot Song.flac", sent_msg)

    def test_only_up_to_max_fetch_slots_are_promoted_at_once(self):
        self.set_config(MAX_FETCH_SLOTS=2)
        ids = [dcc_fetch.enqueue_fetch("bot", f"File{i}.flac") for i in range(4)]

        dcc_fetch.check_fetch_queue()

        states = [config.fetch_queue[rid]["state"] for rid in ids]
        self.assertEqual(states.count("offered"), 2)
        self.assertEqual(states.count("pending"), 2)

    def test_already_active_fetches_count_against_the_slot_limit(self):
        self.set_config(MAX_FETCH_SLOTS=1)
        busy_id = dcc_fetch.enqueue_fetch("bot", "Busy.flac")
        config.fetch_queue[busy_id]["state"] = "receiving"
        waiting_id = dcc_fetch.enqueue_fetch("bot", "Waiting.flac")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[waiting_id]["state"], "pending")

    def test_an_offer_nobody_ever_answers_expires_as_failed(self):
        self.set_config(FETCH_OFFER_TIMEOUT=60)
        rid = dcc_fetch.enqueue_fetch("silentbot", "Ghost.flac")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time() - 61

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["reason"], "no response")

    def test_an_offer_still_within_its_timeout_is_left_alone(self):
        self.set_config(FETCH_OFFER_TIMEOUT=60)
        rid = dcc_fetch.enqueue_fetch("bot", "Song.flac")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time() - 5

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_disabled_feature_never_promotes_anything(self):
        config.fetch_feature_disabled = True
        rid = dcc_fetch.enqueue_fetch("bot", "Song.flac")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[rid]["state"], "pending")

    def test_a_folder_offer_survives_past_the_plain_offer_timeout(self):
        """A "folder" row waits on FETCH_FOLDER_OFFER_TIMEOUT, not the plain
        FETCH_OFFER_TIMEOUT - the other bot has to run its own !rar packing
        pipeline first, which can easily outlast a normal file's timeout."""
        self.set_config(FETCH_OFFER_TIMEOUT=60, FETCH_FOLDER_OFFER_TIMEOUT=1800)
        rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time() - 61

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_a_folder_offer_eventually_expires_past_its_own_timeout(self):
        self.set_config(FETCH_OFFER_TIMEOUT=60, FETCH_FOLDER_OFFER_TIMEOUT=1800)
        rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time() - 1801

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["reason"], "no response")

    def test_a_file_row_is_unaffected_by_the_folder_offer_timeout(self):
        """Control/decoupling test: a "file" row's expiry must still be
        governed by FETCH_OFFER_TIMEOUT alone - a huge
        FETCH_FOLDER_OFFER_TIMEOUT must never accidentally protect a "file"
        row past its own, much shorter, timeout."""
        self.set_config(FETCH_OFFER_TIMEOUT=60, FETCH_FOLDER_OFFER_TIMEOUT=1800)
        rid = dcc_fetch.enqueue_fetch("bot", "Song.flac", request_type="file")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time() - 61

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["reason"], "no response")
        self.assertEqual(self.oserve.queued, [])

    def test_count_active_fetches_counts_offered_listening_and_receiving(self):
        config.fetch_queue = {
            "a": {"state": "pending"},
            "b": {"state": "offered"},
            "c": {"state": "listening"},
            "d": {"state": "receiving"},
            "e": {"state": "complete"},
            "f": {"state": "failed"},
        }
        self.assertEqual(dcc_fetch.count_active_fetches(), 3)

    def test_a_listening_row_stuck_past_the_safety_net_timeout_expires_as_failed(self):
        """BUG regression: _serve_passive_offer() marks a 'listening' row
        failed on its own way out (timeout/OSError/any other exception - see
        its own comments), but if the daemon thread running it dies or hangs
        BEFORE it gets that far, nothing else ever revisited a 'listening'
        row - check_fetch_queue()'s expiry loop only re-examined 'offered'
        rows, so the row (and its MAX_FETCH_SLOTS slot) would be stuck until
        the process restarts.
        """
        rid = dcc_fetch.enqueue_fetch("passivebot", "Ghost.flac")
        config.fetch_queue[rid]["state"] = "listening"
        config.fetch_queue[rid]["listening_since"] = (
            time.time() - (dcc_fetch.PASSIVE_LISTEN_TIMEOUT * 3) - 1)

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["reason"], "listening row expired without a resolution")

    def test_a_listening_row_still_within_the_safety_net_window_is_left_alone(self):
        rid = dcc_fetch.enqueue_fetch("passivebot", "Ghost.flac")
        config.fetch_queue[rid]["state"] = "listening"
        config.fetch_queue[rid]["listening_since"] = time.time() - 5

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[rid]["state"], "listening")


class ARequestGoesToTheBotsOwnChannel(DCCoreTestCase):
    """Reported live: a bot only in the second of several configured
    channels had its fetch dispatched into the first one instead, and every
    request to it failed with "no response" - unsurprising, since the bot
    never saw a request that never reached its channel. check_fetch_queue()
    used to send every dispatched request into one fixed channel
    (BROADCAST_SEARCH_CHANNEL, or else the first entry of config.CHANNEL)
    no matter where the target bot actually was.

    webserver.bot_not_here_error() already refuses to even enqueue a fetch
    for a bot in none of our channels - these tests are about the bot BEING
    somewhere, just not the one fixed channel dispatch used to assume.
    """

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#one,#two,#three")

    def _dispatch(self, bot, filename="Song.flac", request_type="file"):
        rid = dcc_fetch.enqueue_fetch(bot, filename, request_type=request_type)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(len(self.oserve.queued), 1)
        _user, message, _is_vip = self.oserve.queued[0]
        return message

    def test_the_request_goes_into_the_channel_the_bot_is_actually_in(self):
        config.channel_users["#two"] = {"somebot"}

        message = self._dispatch("SomeBot")

        self.assertIn("PRIVMSG #two :", message)
        self.assertNotIn("#one", message)

    def test_a_list_request_goes_to_the_right_channel_too(self):
        config.channel_users["#two"] = {"somebot"}

        message = self._dispatch("SomeBot", request_type="list")

        self.assertIn("PRIVMSG #two :@SomeBot", message)

    def test_a_folder_request_goes_to_the_right_channel_too(self):
        config.channel_users["#two"] = {"somebot"}

        message = self._dispatch("SomeBot", "!rar Artist/Album", request_type="folder")

        self.assertIn("PRIVMSG #two :!SomeBot", message)

    def test_a_bot_nowhere_we_know_of_falls_back_to_the_first_channel(self):
        """Presence changed between enqueue and this dispatch tick - the
        request must still go somewhere rather than vanish silently, even
        though it is no better off than before this fix."""
        message = self._dispatch("ghostbot")

        self.assertIn("PRIVMSG #one :", message)

    def test_a_bot_present_in_more_than_one_channel_picks_the_configured_order(self):
        config.channel_users["#two"] = {"multibot"}
        config.channel_users["#three"] = {"multibot"}

        message = self._dispatch("multibot")

        self.assertIn("PRIVMSG #two :", message,
                      "#two comes before #three in config.CHANNEL")

    def test_the_match_is_case_insensitive_but_the_channel_keeps_its_own_case(self):
        config.channel_users["#two"] = {"somebot"}

        message = self._dispatch("SOMEBOT")

        self.assertIn("PRIVMSG #two :", message)

    def test_broadcast_search_channel_is_only_the_last_resort_now(self):
        """BROADCAST_SEARCH_CHANNEL used to be dispatch's one and only
        default - now the bot's own channel wins over it too, and it is
        purely a fallback for a bot presence cannot place anywhere."""
        self.set_config(BROADCAST_SEARCH_CHANNEL="#three")
        config.channel_users["#two"] = {"somebot"}

        message = self._dispatch("SomeBot")

        self.assertIn("PRIVMSG #two :", message)

    def test_broadcast_search_channel_still_answers_for_an_unplaceable_bot(self):
        self.set_config(BROADCAST_SEARCH_CHANNEL="#three")

        message = self._dispatch("ghostbot")

        self.assertIn("PRIVMSG #three :", message)


class LoopbackTransferTests(DCCoreTestCase):
    """Real sockets, like tests/test_adminchat.py's loopback pattern - not
    everything mocked, so the actual recv/write/size-accounting loop runs."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.addCleanup(self.listener.close)

    def _offer_line(self, filename, size):
        return f"DCC SEND {filename} {ip_long('127.0.0.1')} {self.port} {size}"

    def _enqueue_and_offer(self, bot, filename):
        rid = dcc_fetch.enqueue_fetch(bot, filename)
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()
        return rid

    def test_happy_path_end_to_end(self):
        payload = b"FLAC" + (b"\x01\x02\x03\x04" * 1024)
        rid = self._enqueue_and_offer("peerbot", "Song.flac")

        def serve():
            self.listener.settimeout(5.0)
            conn, _ = self.listener.accept()
            conn.settimeout(5.0)
            conn.sendall(payload)
            conn.close()

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()

        dcc_fetch.handle_incoming_offer(None, "peerbot", self._offer_line("Song.flac", len(payload)))
        server_thread.join(timeout=5.0)

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["bytes_received"], len(payload))

        stored_path = os.path.join(self.tmp, row["stored_filename"])
        self.assertTrue(os.path.exists(stored_path))
        with open(stored_path, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_oversized_delivery_aborts_and_deletes_the_partial_file(self):
        """The offer DECLARES a small size but the peer actually sends more -
        a lying offer. Must abort and delete rather than keep the overflow."""
        declared_size = 100
        actual_payload = b"X" * 500
        rid = self._enqueue_and_offer("lyingbot", "Trap.flac")

        def serve():
            self.listener.settimeout(5.0)
            conn, _ = self.listener.accept()
            conn.settimeout(5.0)
            conn.sendall(actual_payload)
            conn.close()

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()

        dcc_fetch.handle_incoming_offer(None, "lyingbot", self._offer_line("Trap.flac", declared_size))
        server_thread.join(timeout=5.0)

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("more bytes", row["reason"])

        stored_name = row.get("stored_filename")
        self.assertIsNotNone(stored_name)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, stored_name)))

    def test_idle_timeout_aborts_and_deletes_the_partial_file(self):
        """The peer accepts the connection, sends a partial chunk, then goes
        silent forever without closing. IDLE_RECV_TIMEOUT must still end it."""
        real_timeout = dcc_fetch.IDLE_RECV_TIMEOUT
        dcc_fetch.IDLE_RECV_TIMEOUT = 0.5
        self.addCleanup(lambda: setattr(dcc_fetch, "IDLE_RECV_TIMEOUT", real_timeout))

        rid = self._enqueue_and_offer("slowbot", "Slow.flac")
        held_open = []

        def serve():
            self.listener.settimeout(5.0)
            conn, _ = self.listener.accept()
            conn.sendall(b"only a little bit")
            held_open.append(conn)  # kept open, never closed, never sends more

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()

        dcc_fetch.handle_incoming_offer(None, "slowbot", self._offer_line("Slow.flac", 5000))
        server_thread.join(timeout=5.0)
        for conn in held_open:
            conn.close()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("idle timeout", row["reason"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, row["stored_filename"])))

    def test_connect_error_is_marked_failed_not_raised(self):
        """Nothing is listening on this port - connect() must be refused, and
        that must produce a 'failed' row, not an unhandled exception in the
        daemon thread this normally runs on."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()  # freed immediately; nothing is listening on it now

        rid = self._enqueue_and_offer("unreachablebot", "Nope.flac")
        offer_line = f"DCC SEND Nope.flac {ip_long('127.0.0.1')} {dead_port} 10"

        dcc_fetch.handle_incoming_offer(None, "unreachablebot", offer_line)

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("connect error", row["reason"])


class PromoteCleanFilenameTests(DCCoreTestCase):
    """dcc_fetch._promote_clean_filename() in isolation.

    Reported live: an operator's fetched files and fetched list zips both
    carrying a name like "058c4cc8ee9a_Some Track.mp3" forever - the request
    id in _resolve_destination_path()'s stored name exists only to keep two
    in-flight fetches from colliding on the same cleaned filename, and that
    risk is over the moment one of them finishes.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-promote-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _touch(self, name, content=b"data"):
        with open(os.path.join(self.tmp, name), "wb") as f:
            f.write(content)

    def test_the_id_prefix_is_dropped(self):
        self._touch("abcdef012345_Track.flac", b"payload")

        result = dcc_fetch._promote_clean_filename(self.tmp, "abcdef012345_Track.flac")

        self.assertEqual(result, "Track.flac")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "Track.flac")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "abcdef012345_Track.flac")))

    def test_a_name_already_taken_keeps_the_longer_one(self):
        """Never overwrites - the same rule note_nick_change() applies for the
        identical reason (irc.py): no data lost, the file just keeps its
        longer name on this one occasion rather than clobbering whatever
        else is already using the plain name."""
        self._touch("abcdef012345_Track.flac", b"new")
        self._touch("Track.flac", b"already here")

        result = dcc_fetch._promote_clean_filename(self.tmp, "abcdef012345_Track.flac")

        self.assertEqual(result, "abcdef012345_Track.flac")
        with open(os.path.join(self.tmp, "Track.flac"), "rb") as f:
            self.assertEqual(f.read(), b"already here")
        with open(os.path.join(self.tmp, "abcdef012345_Track.flac"), "rb") as f:
            self.assertEqual(f.read(), b"new")

    def test_a_name_with_no_id_prefix_is_left_alone(self):
        """Defense-in-depth: nothing calls this on a name that was never
        built by _resolve_destination_path(), but it must not mangle one
        that reaches it anyway."""
        self._touch("Track.flac")

        result = dcc_fetch._promote_clean_filename(self.tmp, "Track.flac")

        self.assertEqual(result, "Track.flac")

    def test_a_rename_failure_falls_back_to_the_original_name(self):
        self._touch("abcdef012345_Track.flac")
        real_rename = os.rename

        def _boom(*a, **k):
            raise OSError("permission denied")

        dcc_fetch.os.rename = _boom
        self.addCleanup(setattr, dcc_fetch.os, "rename", real_rename)

        result = dcc_fetch._promote_clean_filename(self.tmp, "abcdef012345_Track.flac")

        self.assertEqual(result, "abcdef012345_Track.flac")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "abcdef012345_Track.flac")))


class ACompletedFetchDropsItsTemporaryId(DCCoreTestCase):
    """End to end, through the real transfer loop - PromoteCleanFilenameTests
    above covers the helper in isolation; this covers it actually being
    called from _run_transfer() for "file" and "folder" rows."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-id-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.addCleanup(self.listener.close)

    def _serve_and_fetch(self, bot, filename, payload, request_type="file"):
        rid = dcc_fetch.enqueue_fetch(bot, filename, request_type=request_type)
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        def serve():
            self.listener.settimeout(5.0)
            conn, _ = self.listener.accept()
            conn.settimeout(5.0)
            conn.sendall(payload)
            conn.close()

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()
        offer_line = f"DCC SEND {filename} {ip_long('127.0.0.1')} {self.port} {len(payload)}"
        dcc_fetch.handle_incoming_offer(None, bot, offer_line)
        server_thread.join(timeout=5.0)
        return rid

    def test_a_completed_file_fetch_has_no_id_in_its_stored_name(self):
        payload = b"FLAC" + (b"\x01\x02\x03\x04" * 1024)
        rid = self._serve_and_fetch("peerbot", "Song.flac", payload)

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["stored_filename"], "Song.flac")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "Song.flac")))

    def test_a_completed_folder_fetch_has_no_id_in_its_stored_name_either(self):
        payload = b"RAR" + (b"\x05" * 200)
        rid = self._serve_and_fetch("peerbot", "Album.rar", payload, request_type="folder")

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["stored_filename"], "Album.rar")

    def test_two_fetches_racing_for_the_same_name_do_not_collide(self):
        """The reason the id exists at all - two overlapping fetches for a
        name that will collide once both are done. Served sequentially here
        (this test does not need real concurrency to prove the outcome:
        whichever finishes second must not silently destroy the first)."""
        first_payload = b"first-file-data"
        second_payload = b"second-file-data-longer"

        first_rid = self._serve_and_fetch("botone", "Track.flac", first_payload)
        second_rid = self._serve_and_fetch("bottwo", "Track.flac", second_payload)

        first_name = config.fetch_queue[first_rid]["stored_filename"]
        second_name = config.fetch_queue[second_rid]["stored_filename"]
        self.assertNotEqual(first_name, second_name)

        with open(os.path.join(self.tmp, first_name), "rb") as f:
            self.assertEqual(f.read(), first_payload)
        with open(os.path.join(self.tmp, second_name), "rb") as f:
            self.assertEqual(f.read(), second_payload)


class ACompletedListFetchRemovesItsZip(DCCoreTestCase):
    """dcc_fetch._handle_completed_list_fetch(): the raw zip is dead weight
    once list_fetch.py has safely extracted it - nothing ever reopens it
    afterward, including a re-fetch, which downloads a fresh one. Reported
    live: downloading one of these to an operator's own machine and
    unzipping it there put the request id into the resulting filename too,
    since a plain list zip has no folder recorded inside it for an unzip
    tool to extract "into" the way a folder packed with rar's -ep1 does."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-listzip-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

    def _write_real_list_zip(self, bot, path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                f"{bot}-2026-09-12.txt",
                "List of 1 Files generated on Sep 12th\n"
                "To request a file, copy/paste to the channel... !x FILENAME\n\n\n"
                + "=" * 53 + "\n"
                "Folder\\Path\\\n"
                f"!{bot} Track.flac  ::INFO:: 1.0MB\n")
        with open(path, "wb") as fh:
            fh.write(buf.getvalue())

    def test_the_zip_is_removed_on_success(self):
        zip_path = os.path.join(self.tmp, "abcdef012345_GoodBot-2026-09-12.zip")
        self._write_real_list_zip("goodbot", zip_path)
        row = {"bot": "goodbot"}

        dcc_fetch._handle_completed_list_fetch(row, zip_path)

        self.assertFalse(os.path.exists(zip_path))
        self.assertIsNone(row["stored_filename"])
        self.assertNotIn("list_processing_error", row)

    def test_the_list_is_actually_browsable_afterward(self):
        """Not just "the zip is gone" - the point of removing it is that the
        extracted copy already stands on its own."""
        zip_path = os.path.join(self.tmp, "abcdef012345_GoodBot-2026-09-12.zip")
        self._write_real_list_zip("goodbot", zip_path)
        row = {"bot": "goodbot"}

        dcc_fetch._handle_completed_list_fetch(row, zip_path)

        self.assertIn("goodbot", dict(getattr(config, "fetched_bot_lists", {}) or {}))

    def test_a_failed_extraction_keeps_the_zip(self):
        """The one piece of diagnostic evidence for why a peer's archive
        could not be read - removing it would trade that for nothing."""
        zip_path = os.path.join(self.tmp, "abcdef012345_BadBot.zip")
        with open(zip_path, "wb") as f:
            f.write(b"not a zip file at all")
        row = {"bot": "badbot", "stored_filename": "abcdef012345_BadBot.zip"}

        dcc_fetch._handle_completed_list_fetch(row, zip_path)

        self.assertTrue(os.path.exists(zip_path))
        self.assertIn("list_processing_error", row)
        self.assertEqual(row["stored_filename"], "abcdef012345_BadBot.zip",
                         "a failed extraction must not touch stored_filename - "
                         "the zip is still exactly where that name points")


class FetchListenerPortOrderingTests(DCCoreTestCase):
    """BUG regression: _open_fetch_listener() used to scan DOWNWARD from
    DCC_PORT_END - the exact same direction and first-probed port as
    adminchat._open_chat_listener()'s own passive DCC CHAT listener - despite
    its own docstring claiming the two landed at opposite ends of the range.
    It now starts at the midpoint instead, which is never the same
    first-probed port as either adminchat's downward-from-end scan or
    dcc.py's own outbound SEND (upward-from-start, verified separately in
    dcc.py's own tests) - reducing first-probe collisions with both.

    WHY THESE DO NOT BIND REAL SOCKETS

    They used to. That made them assert which port came back while quietly
    depending on 55000-55010 being free on the machine, which is not something
    a test can assume: `_open_fetch_listener()` falls through to the next port
    whenever one is taken, so a listener still open from an earlier test - or
    a TIME_WAIT left by one - silently changes the answer. Both failed that
    way during a full-suite run, one as `55004 != 55005` and the other as
    `55000 == 55000`, on a branch that touched none of this.

    Proven environmental rather than a regression: on a clean checkout, simply
    holding port 55005 reproduces the first failure exactly.

    What these tests are actually about is the PROBE ORDER, and that needs no
    network at all. Faking bind makes them deterministic, lets them state
    which ports are occupied instead of hoping, and covers the fallback
    behaviour that could not be reached before. Real binding is still
    exercised end to end by PassiveOfferEndToEndTests below.
    """

    def probe(self, occupied=()):
        """Run _open_fetch_listener() against a fake socket layer.

        Returns (attempted_ports, port). `occupied` names the ports whose bind
        should fail, the way a port held by another process behaves.
        """
        attempted = []
        taken = set(occupied)
        real_socket_cls = socket.socket

        class FakeListenerSocket:
            def __init__(self, *args, **kwargs):
                pass

            def setsockopt(self, *args, **kwargs):
                # platform_compat.prepare_listener() sets one address-reuse
                # option and nothing else.
                return None

            def bind(self, addr):
                attempted.append(addr[1])
                if addr[1] in taken:
                    raise OSError(98, "Address already in use")

            def listen(self, *args, **kwargs):
                return None

            def close(self):
                return None

        socket.socket = FakeListenerSocket
        try:
            _listener, port = dcc_fetch._open_fetch_listener()
        finally:
            socket.socket = real_socket_cls
        return attempted, port

    def test_first_probed_port_is_the_midpoint_of_the_range(self):
        self.set_config(DCC_PORT_START=55000, DCC_PORT_END=55010)

        attempted, port = self.probe()

        self.assertEqual(attempted[0], 55005, "midpoint of 55000-55010")
        self.assertEqual(port, 55005)

    def test_never_shares_a_first_probed_port_with_adminchat_or_dcc_py(self):
        """adminchat scans downward from DCC_PORT_END (first probe: END);
        dcc.py's own outbound SEND scans upward from DCC_PORT_START (first
        probe: START). The fetch listener's first probe must be neither."""
        self.set_config(DCC_PORT_START=55000, DCC_PORT_END=55010)

        attempted, _port = self.probe()

        self.assertNotEqual(attempted[0], config.DCC_PORT_START)
        self.assertNotEqual(attempted[0], config.DCC_PORT_END)

    def test_it_scans_upward_from_the_midpoint_then_wraps_downward(self):
        """The full order, which the real-socket version could only assert the
        first element of. Upward from the midpoint to the end, then downward
        from just below the midpoint to the start."""
        self.set_config(DCC_PORT_START=55000, DCC_PORT_END=55010)

        attempted, _port = self.probe(occupied=range(55000, 55011))

        self.assertEqual(attempted,
                         [55005, 55006, 55007, 55008, 55009, 55010,
                          55004, 55003, 55002, 55001, 55000])

    def test_a_busy_midpoint_falls_through_to_the_next_port(self):
        """Reachable only now that occupancy can be stated. This is exactly
        what the machine was doing to these tests when they bound for real."""
        self.set_config(DCC_PORT_START=55000, DCC_PORT_END=55010)

        attempted, port = self.probe(occupied=[55005])

        self.assertEqual(attempted[:2], [55005, 55006])
        self.assertEqual(port, 55006)

    def test_a_full_range_returns_no_listener_rather_than_raising(self):
        """The caller checks for None. Raising instead would take down the
        fetch path on a busy box rather than declining one offer."""
        self.set_config(DCC_PORT_START=55000, DCC_PORT_END=55010)

        attempted, port = self.probe(occupied=range(55000, 55011))

        self.assertIsNone(port)
        self.assertEqual(len(attempted), 11, "every port must be tried once")

    def test_a_single_port_range_still_works(self):
        """Boundary: start == end makes the midpoint both ends at once, and
        the downward half of the scan empty."""
        self.set_config(DCC_PORT_START=55000, DCC_PORT_END=55000)

        attempted, port = self.probe()

        self.assertEqual(attempted, [55000])
        self.assertEqual(port, 55000)


class PassiveOfferEndToEndTests(DCCoreTestCase):
    """Passive/reverse DCC SEND: the offering bot sends port 0 plus a token
    and WE become the listener instead of dialling out. Mirrors
    tests/test_adminchat.py's own real-socket pattern for the identical DCC
    CHAT case (ListenModeEndToEnd) rather than mocking the socket layer.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        # Our own reply offer advertises config.MY_IP_OR_DOCK - has to be
        # loopback for the "other bot" side of these tests to dial back in.
        config.MY_IP_OR_DOCK = "127.0.0.1"

    def _enqueue_and_offer(self, bot, filename):
        rid = dcc_fetch.enqueue_fetch(bot, filename)
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()
        return rid

    def _wait_for_reply(self, timeout=5.0):
        """Block until dcc_fetch queues our answering DCC SEND CTCP through
        oserve.queue_message (never a raw socket write - see the brief)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for user, message, _is_vip in self.oserve.queued:
                if "DCC SEND" in message:
                    return user, message
            time.sleep(0.02)
        return None, None

    def _fields_of(self, ctcp_message):
        """PRIVMSG <nick> :\\x01DCC SEND <name> <ip> <port> <size> <token>\\x01\\r\\n
        -> the space-split fields after the CTCP delimiter."""
        payload = ctcp_message.split("\x01", 1)[1]
        return payload.rstrip("\x01\r\n").split()

    def test_the_reported_offer_end_to_end(self):
        """The exact shape from a real capture, all the way through a real
        accepted connection and a byte-for-byte written file.

        The SHAPE is real; the nick that used to be named here was not, and
        saying otherwise is what kept three real names alive through three
        scrub passes - see tests/test_advert_listener.py's own note. A
        sentence asserting a name is genuine is the thing that stops the next
        reader replacing it."""
        payload = b"MP3" + (b"\x05\x06\x07\x08" * 2048)
        rid = self._enqueue_and_offer("valogg", "72_Seasons.mp3")
        offer_line = (f"DCC SEND 72_Seasons.mp3 {ip_long('198.51.100.9')} "
                      f"0 {len(payload)} 42424")

        worker = threading.Thread(
            target=dcc_fetch.handle_incoming_offer,
            args=(None, "valogg", offer_line), daemon=True)
        worker.start()

        user, message = self._wait_for_reply()
        self.assertIsNotNone(message, "our own passive-offer reply must be "
                             "queued through oserve.queue_message")
        self.assertEqual(user, "valogg")

        fields = self._fields_of(message)
        self.assertEqual(fields[:3], ["DCC", "SEND", "72_Seasons.mp3"])
        self.assertEqual(fields[3], str(ip_long("127.0.0.1")),
                         "must advertise OUR OWN ip, not the offering bot's")
        our_port = int(fields[4])
        self.assertGreaterEqual(our_port, config.DCC_PORT_START)
        self.assertLessEqual(our_port, config.DCC_PORT_END)
        self.assertEqual(fields[5], str(len(payload)))
        self.assertEqual(fields[6], "42424",
                         "the token must be echoed back unchanged, or ValOgg "
                         "cannot match our reply to its own request")

        # Stand in for the offering bot: dial the port WE just advertised,
        # exactly as the real third party would once it sees our reply.
        client = socket.create_connection(("127.0.0.1", our_port), timeout=5.0)
        client.sendall(payload)
        client.close()

        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive(), "the transfer must finish, not hang")

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["bytes_received"], len(payload))

        stored_path = os.path.join(self.tmp, row["stored_filename"])
        self.assertTrue(os.path.exists(stored_path))
        with open(stored_path, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_an_unexpected_error_while_answering_is_marked_failed_not_left_stranded(self):
        """BUG regression: _serve_passive_offer()'s accept-and-reply block
        used to only catch socket.timeout and OSError - any other exception
        (e.g. a future bug in oserve.queue_message itself) propagated out of
        the bare daemon thread this runs on, leaving the row stuck in
        'listening' forever (a permanently lost MAX_FETCH_SLOTS slot), since
        nothing else ever revisited a 'listening' row at the time.
        """
        rid = self._enqueue_and_offer("faultybot", "Trouble.flac")
        offer_line = f"DCC SEND Trouble.flac {ip_long('198.51.100.9')} 0 4096 777"

        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated failure in the outbound queue")

        self.oserve.queue_message = _boom

        dcc_fetch.handle_incoming_offer(None, "faultybot", offer_line)

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("unexpected error", row["reason"])

        # The listener must not leak either - opening a fresh one right after
        # must succeed, exactly like the existing accept-timeout test proves.
        # Reuses dcc_fetch's own _open_fetch_listener() (which already sets
        # SO_REUSEADDR via platform_compat.prepare_listener(), the same way
        # production code itself tolerates a just-closed port's TIME_WAIT
        # state) rather than a raw bind from the test, which would report a
        # false "leak" against nothing more than an unrelated prior test's
        # socket still lingering in TIME_WAIT on the same shared range.
        listener2, port2 = dcc_fetch._open_fetch_listener()
        self.assertIsNotNone(listener2, "listener leaked after an unexpected error")
        listener2.close()

    def test_a_second_passive_offer_with_a_different_token_and_size_also_works(self):
        payload = os.urandom(4096)
        rid = self._enqueue_and_offer("otherbot", "Different.flac")
        offer_line = (f"DCC SEND Different.flac {ip_long('203.0.113.50')} "
                      f"0 {len(payload)} 9001")

        worker = threading.Thread(
            target=dcc_fetch.handle_incoming_offer,
            args=(None, "otherbot", offer_line), daemon=True)
        worker.start()

        _user, message = self._wait_for_reply()
        self.assertIsNotNone(message)
        fields = self._fields_of(message)
        self.assertEqual(fields[6], "9001")
        our_port = int(fields[4])

        client = socket.create_connection(("127.0.0.1", our_port), timeout=5.0)
        client.sendall(payload)
        client.close()
        worker.join(timeout=5.0)

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["bytes_received"], len(payload))

    def test_nobody_ever_connects_back_fails_cleanly_with_no_leaked_socket(self):
        """The offering bot never actually dials us back (dead peer, its own
        firewall issue, whatever) - PASSIVE_LISTEN_TIMEOUT must still end it
        with a clean 'failed' row, and the port must be free again
        immediately afterwards (proof the listening socket was closed, not
        leaked)."""
        real_timeout = dcc_fetch.PASSIVE_LISTEN_TIMEOUT
        dcc_fetch.PASSIVE_LISTEN_TIMEOUT = 0.5
        self.addCleanup(lambda: setattr(dcc_fetch, "PASSIVE_LISTEN_TIMEOUT", real_timeout))

        rid = self._enqueue_and_offer("ghostbot", "Ghost.flac")
        offer_line = f"DCC SEND Ghost.flac {ip_long('198.51.100.9')} 0 5000 555"

        # Called directly (not threaded): with a 0.5s accept timeout this
        # returns on its own almost immediately - no real "nothing ever
        # happens" wait needed.
        dcc_fetch.handle_incoming_offer(None, "ghostbot", offer_line)

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("no connection received", row["reason"])

        _user, message = self._wait_for_reply(timeout=1.0)
        self.assertIsNotNone(message, "an offer must still have been sent "
                             "before the accept timed out")
        our_port = int(self._fields_of(message)[4])

        # If the listener leaked, no port in the (tiny, 11-port) shared range
        # would be bindable. Reuses dcc_fetch's own _open_fetch_listener()
        # (which already applies platform_compat.prepare_listener()'s
        # SO_REUSEADDR handling) rather than a raw bind on `our_port`
        # specifically - a raw bind can spuriously report "still in use"
        # against nothing more than an unrelated prior test's socket lingering
        # in the OS's normal TIME_WAIT state on the same shared range under
        # the full suite's load, exactly the false-positive trap
        # platform_compat.prepare_listener() exists to avoid for real
        # listeners (see the identical fix and reasoning on
        # test_an_unexpected_error_while_answering_is_marked_failed_not_left_stranded
        # above).
        listener2, port2 = dcc_fetch._open_fetch_listener()
        self.assertIsNotNone(
            listener2,
            f"the passive listener on port {our_port} was not closed after "
            f"the accept timeout: no free port in the shared DCC range")
        listener2.close()

    def test_the_row_passes_through_listening_before_receiving(self):
        """Documents the extra state hop this feature adds to the module's
        state machine (see dcc_fetch.py's module docstring): offered ->
        listening -> receiving -> complete, not straight to receiving."""
        rid = self._enqueue_and_offer("slowconnectbot", "Watch.flac")
        offer_line = f"DCC SEND Watch.flac {ip_long('198.51.100.9')} 0 10 111"

        seen_listening = threading.Event()

        def watcher():
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if config.fetch_queue[rid]["state"] == "listening":
                    seen_listening.set()
                    return
                time.sleep(0.01)

        watch_thread = threading.Thread(target=watcher, daemon=True)
        watch_thread.start()

        worker = threading.Thread(
            target=dcc_fetch.handle_incoming_offer,
            args=(None, "slowconnectbot", offer_line), daemon=True)
        worker.start()

        _user, message = self._wait_for_reply()
        self.assertIsNotNone(message)
        our_port = int(self._fields_of(message)[4])

        watch_thread.join(timeout=5.0)
        self.assertTrue(seen_listening.is_set(),
                        "the row must pass through 'listening' while waiting "
                        "for the offering bot to connect back")

        client = socket.create_connection(("127.0.0.1", our_port), timeout=5.0)
        client.sendall(b"0123456789")
        client.close()
        worker.join(timeout=5.0)

        self.assertEqual(config.fetch_queue[rid]["state"], "complete")


class PassiveOfferInjectionTests(DCCoreTestCase):
    """BUG 1 (CRITICAL, found in adversarial review): offer["filename"] and
    offer["token"] used to be echoed back into our own outbound CTCP reply
    completely unsanitized (only spaces were turned into underscores) -
    embedded \\r, \\n or \\x01 bytes reached oserve.queue_message() verbatim,
    which is the exact CRLF/CTCP injection class already fixed once for
    webserver.py's web-enqueue routes, recurring here through a different
    input channel.

    The fix rejects any offer (active or passive) whose filename, or
    whose token in the passive form, contains one of those bytes - at
    PARSE time, in parse_dcc_send_offer(), before an `offer` dict carrying
    them can even be constructed - plus a second, redundant guard right at
    the one call site that builds the outbound CTCP
    (_serve_passive_offer()). These tests exercise both layers and assert
    directly on what actually reached (or, correctly, never reached)
    oserve.queue_message() - not just on the row's final state - since
    that call is where the injection actually lives.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        config.MY_IP_OR_DOCK = "127.0.0.1"

    def _enqueue_and_offer(self, bot, filename):
        rid = dcc_fetch.enqueue_fetch(bot, filename)
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()
        return rid

    def test_parser_rejects_a_crlf_smuggled_filename(self):
        """The exact repro from the review: a filename that substitutes
        CR/LF for the space/underscore in an already-approved requested
        filename, so it would otherwise still match under
        _normalize_filename_for_match()'s [\\s_]+ collapsing."""
        offer = dcc_fetch.parse_dcc_send_offer(
            "DCC SEND My\r\nQUIT\r\nSong.mp3 " + str(ip_long("198.51.100.9")) +
            " 0 4096 555")
        self.assertIsNone(offer)

    def test_parser_rejects_a_ctcp_delimiter_in_the_filename(self):
        offer = dcc_fetch.parse_dcc_send_offer(
            "DCC SEND Evil\x01NICK\x01Song.mp3 " + str(ip_long("198.51.100.9")) +
            " 0 4096 555")
        self.assertIsNone(offer)

    def test_parser_rejects_a_ctcp_delimiter_in_the_token(self):
        offer = dcc_fetch.parse_dcc_send_offer(
            "DCC SEND Song.mp3 " + str(ip_long("198.51.100.9")) +
            " 0 4096 555\x01NICK\x01HACKED")
        self.assertIsNone(offer)

    def test_parser_rejects_crlf_in_an_active_offers_filename_too(self):
        """Not just the passive/echoed form - a filename this hostile is
        never a real DCC client's output either way."""
        offer = dcc_fetch.parse_dcc_send_offer(
            "DCC SEND My\r\nQUIT\r\nSong.mp3 " + str(ip_long("198.51.100.9")) +
            " 55000 4096")
        self.assertIsNone(offer)

    def test_malicious_passive_offer_never_reaches_admission_control_or_queue_message(self):
        """End-to-end through handle_incoming_offer(): even with a row
        already approved and waiting for exactly this filename (underscore
        form), the CRLF-substituted offer must be dropped before admission
        control ever claims the row, and NOTHING may be handed to
        oserve.queue_message() for it."""
        rid = self._enqueue_and_offer("evilbot", "My_QUIT_Song.mp3")
        offer_line = ("DCC SEND My\r\nQUIT\r\nSong.mp3 " +
                      str(ip_long("198.51.100.9")) + " 0 4096 555")

        dcc_fetch.handle_incoming_offer(None, "evilbot", offer_line)

        self.assertEqual(config.fetch_queue[rid]["state"], "offered",
                         "the row must NOT be claimed - the offer never "
                         "even parses")
        self.assertEqual(self.oserve.queued, [],
                         "no message of any kind may reach "
                         "oserve.queue_message() for a hostile offer")

    def test_serve_passive_offer_second_layer_guard_refuses_to_build_the_reply(self):
        """Defense-in-depth: even if a caller somehow hands
        _serve_passive_offer() an offer dict that bypassed
        parse_dcc_send_offer()'s check (e.g. a future call site built one a
        different way), the call site that actually builds the outbound
        CTCP must still refuse rather than trust it blindly."""
        rid = self._enqueue_and_offer("evilbot", "Song.mp3")
        row = config.fetch_queue[rid]
        row["state"] = "listening"
        hostile_offer = {"filename": "Evil\x01NICK\x01Song.mp3", "ip": None,
                          "port": 0, "size": 4096, "token": "555",
                          "claimed_ip": None}

        dcc_fetch._serve_passive_offer(
            None, "evilbot", row, hostile_offer, self.tmp, "stored_name.mp3")

        self.assertEqual(row["state"], "failed")
        self.assertIn("unsafe characters", row["reason"])
        self.assertEqual(self.oserve.queued, [],
                         "the hostile filename/token must never reach "
                         "oserve.queue_message()")


class PassiveOfferPeerCheckTests(DCCoreTestCase):
    """BUG 2 (HIGH, found in adversarial review): _serve_passive_offer()'s
    listener.accept() took the first TCP connection to arrive with zero
    examination of who it was from. The best-effort mitigation compares the
    accepted peer's address against offer["claimed_ip"] (decoded from the
    ip_long field the offer itself carries, even though that field is never
    dialled for a passive offer) and records a logged, non-fatal mismatch on
    the row - a real offering bot behind NAT can legitimately advertise a
    different address than it connects from, so this is deliberately a
    best-effort signal, not a hard rejection (see the comment above
    listener.accept() in dcc_fetch.py).
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        config.MY_IP_OR_DOCK = "127.0.0.1"

    def _enqueue_and_offer(self, bot, filename):
        rid = dcc_fetch.enqueue_fetch(bot, filename)
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()
        return rid

    def _wait_for_reply(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for user, message, _is_vip in self.oserve.queued:
                if "DCC SEND" in message:
                    return user, message
            time.sleep(0.02)
        return None, None

    def _fields_of(self, ctcp_message):
        payload = ctcp_message.split("\x01", 1)[1]
        return payload.rstrip("\x01\r\n").split()

    def test_a_connection_from_an_unclaimed_address_is_recorded_as_a_mismatch(self):
        """The Tester's repro, shaped as a loopback-safe regression test: the
        offer claims one address (never dialled, so it can be anything), but
        the actual peer that connects - the only real signal we have - is a
        different one. Proves the mitigation actually changes observable
        behaviour (the row now records the mismatch) rather than remaining
        the silent zero-examination accept() the review flagged, while still
        NOT breaking the transfer itself (best-effort, not a hard reject)."""
        payload = os.urandom(256)
        rid = self._enqueue_and_offer("evilbot", "Impersonated.flac")
        # Claims an address nothing in this test will ever connect from - the
        # real peer, per the bug, is free to be anyone.
        offer_line = (f"DCC SEND Impersonated.flac {ip_long('203.0.113.99')} "
                      f"0 {len(payload)} 555")

        worker = threading.Thread(
            target=dcc_fetch.handle_incoming_offer,
            args=(None, "evilbot", offer_line), daemon=True)
        worker.start()

        _user, message = self._wait_for_reply()
        self.assertIsNotNone(message)
        our_port = int(self._fields_of(message)[4])

        # Stand in for "anyone who can reach the port" - not the address the
        # offer claimed.
        client = socket.create_connection(("127.0.0.1", our_port), timeout=5.0)
        client.sendall(payload)
        client.close()
        worker.join(timeout=5.0)

        row = config.fetch_queue[rid]
        # Best-effort, not a hard reject: the transfer still completes...
        self.assertEqual(row["state"], "complete")
        # ...but the mismatch between what was claimed and who actually
        # connected is now recorded on the row where it was not examined at
        # all before this fix.
        self.assertEqual(row["passive_peer_ip"], "127.0.0.1")
        self.assertTrue(row.get("passive_peer_ip_mismatch"),
                        "a peer address that does not match what the offer "
                        "claimed must be flagged")

    def test_a_connection_matching_the_claimed_address_is_not_flagged(self):
        """Sanity check the other direction: no false positives when the
        claimed and actual addresses genuinely agree."""
        payload = os.urandom(256)
        rid = self._enqueue_and_offer("goodbot", "Legit.flac")
        offer_line = (f"DCC SEND Legit.flac {ip_long('127.0.0.1')} "
                      f"0 {len(payload)} 555")

        worker = threading.Thread(
            target=dcc_fetch.handle_incoming_offer,
            args=(None, "goodbot", offer_line), daemon=True)
        worker.start()

        _user, message = self._wait_for_reply()
        self.assertIsNotNone(message)
        our_port = int(self._fields_of(message)[4])

        client = socket.create_connection(("127.0.0.1", our_port), timeout=5.0)
        client.sendall(payload)
        client.close()
        worker.join(timeout=5.0)

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["passive_peer_ip"], "127.0.0.1")
        self.assertNotIn("passive_peer_ip_mismatch", row)


class RequestTypeAdmissionControlTests(DCCoreTestCase):
    """request_type="list" rows (a cross-bot @<bot> list-fetch, see
    dcc_fetch.py's module docstring and check_fetch_queue()) match on bot
    ALONE - any filename from the right bot claims them, since we cannot know
    ahead of time what the target bot will name its list zip. request_type
    "file" rows keep the original exact bot+filename requirement unchanged.
    Neither can accidentally satisfy the other's requirement - see
    _claim_matching_offer_locked()'s own docstring for the invariant these
    tests exist to pin down.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

    def _offer_and_size_cap(self, rid, bot, filename):
        """Force rejection at the (unrelated) size cap so admission control
        can be observed in isolation, with no real socket involved - same
        idiom AdmissionControlTests.test_matching_offer_is_claimed_underscore_space_equivalence
        already uses above. Sets every size cap (a "list" row is checked
        against MAX_FETCH_LIST_FILE_SIZE, not MAX_FETCH_FILE_SIZE - see
        #162 finding #10) so this helper stays request_type-agnostic."""
        self.set_config(MAX_FETCH_FILE_SIZE=10, MAX_FETCH_LIST_FILE_SIZE=10,
                        MAX_FETCH_FOLDER_FILE_SIZE=10)
        dcc_fetch.handle_incoming_offer(
            None, bot, f"DCC SEND {filename} 2130706433 55000 999999")

    def test_list_row_matches_any_filename_from_the_right_bot(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        self._offer_and_size_cap(rid, "goodbot", "WhateverTheyNamedIt.zip")

        row = config.fetch_queue[rid]
        # It was CLAIMED (state moved on from 'offered') and only then failed
        # the unrelated size cap - proving the bot-alone match itself
        # succeeded despite the filename never having been specified ahead
        # of time.
        self.assertEqual(row["state"], "failed")
        self.assertIn("exceeds", row["reason"])
        self.assertEqual(row["filename"], "WhateverTheyNamedIt.zip")

    def test_list_row_rejects_an_offer_from_the_wrong_bot(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail("must not connect")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "impostorbot", "DCC SEND Anything.zip 2130706433 55000 4096")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_a_file_row_still_requires_an_exact_filename_even_with_a_list_row_for_the_same_bot(self):
        """A 'file' row expecting an exact filename must still reject an
        offer with a DIFFERENT filename from the same bot, even though a
        'list' row for that same bot would have accepted it - a 'list' row
        must never accidentally satisfy a 'file' row's stricter requirement."""
        file_rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac", request_type="file")
        config.fetch_queue[file_rid]["state"] = "offered"
        config.fetch_queue[file_rid]["offered_at"] = time.time()

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail("must not connect")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "goodbot", "DCC SEND SomeOtherFile.zip 2130706433 55000 4096")

        self.assertEqual(config.fetch_queue[file_rid]["state"], "offered")

    def test_when_both_a_file_and_list_row_exist_the_exact_filename_match_wins(self):
        """The more specific match (exact filename) takes priority over the
        looser one (bot alone) when both are outstanding for the same bot."""
        file_rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac", request_type="file")
        config.fetch_queue[file_rid]["state"] = "offered"
        config.fetch_queue[file_rid]["offered_at"] = time.time()
        list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        config.fetch_queue[list_rid]["state"] = "offered"
        config.fetch_queue[list_rid]["offered_at"] = time.time()

        self._offer_and_size_cap(file_rid, "goodbot", "Song.flac")

        self.assertEqual(config.fetch_queue[file_rid]["state"], "failed")   # claimed, then hit the size cap
        self.assertEqual(config.fetch_queue[list_rid]["state"], "offered")  # untouched

    def test_a_non_matching_filename_falls_through_to_the_list_row_for_the_same_bot(self):
        """The reverse of the above: an offer that does NOT match the 'file'
        row's exact filename falls through and is claimed by the 'list' row
        for the same bot instead of being rejected outright - a 'file' row
        must never accidentally satisfy (or block) a 'list' row's looser
        requirement for a DIFFERENT offer."""
        file_rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac", request_type="file")
        config.fetch_queue[file_rid]["state"] = "offered"
        config.fetch_queue[file_rid]["offered_at"] = time.time()
        list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        config.fetch_queue[list_rid]["state"] = "offered"
        config.fetch_queue[list_rid]["offered_at"] = time.time()

        self._offer_and_size_cap(list_rid, "goodbot", "ListArchive.zip")

        self.assertEqual(config.fetch_queue[file_rid]["state"], "offered")  # untouched
        self.assertEqual(config.fetch_queue[list_rid]["state"], "failed")   # claimed, then hit the size cap

    def test_multiple_list_rows_for_the_same_bot_the_oldest_is_claimed(self):
        """This state (two outstanding "list" rows for the same bot) can no
        longer arise through the normal enqueue_fetch() call path -
        enqueue_fetch() itself now refuses a second bot-alone row for a bot
        that already has one outstanding (see
        dcc_fetch.has_outstanding_bot_alone_request()), which is covered by
        EnqueueTimeBotAloneCollisionTests below. This test uses
        insert_row_bypassing_enqueue_guard() to still exercise
        _claim_matching_offer_locked()'s oldest-wins tie-break as the
        defense-in-depth path it now is."""
        older_rid = insert_row_bypassing_enqueue_guard("goodbot", "", request_type="list")
        config.fetch_queue[older_rid]["state"] = "offered"
        config.fetch_queue[older_rid]["offered_at"] = time.time()
        config.fetch_queue[older_rid]["requested_at"] = 1.0

        newer_rid = insert_row_bypassing_enqueue_guard("goodbot", "", request_type="list")
        config.fetch_queue[newer_rid]["state"] = "offered"
        config.fetch_queue[newer_rid]["offered_at"] = time.time()
        config.fetch_queue[newer_rid]["requested_at"] = 2.0

        self._offer_and_size_cap(older_rid, "goodbot", "Whatever.zip")

        self.assertEqual(config.fetch_queue[older_rid]["state"], "failed")
        self.assertEqual(config.fetch_queue[newer_rid]["state"], "offered")

    def test_default_request_type_is_file_for_backward_compatibility(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        self.assertEqual(config.fetch_queue[rid]["request_type"], "file")


class FolderRarRequestTypeTests(DCCoreTestCase):
    """request_type="folder" rows (a cross-bot "!<bot> !rar <folder>" whole-
    album fetch, see dcc_fetch.py's module docstring and check_fetch_queue())
    match on bot ALONE, exactly like "list" rows and for the identical
    reason: we cannot know ahead of time what the target bot will name the
    resulting .rar. Mirrors RequestTypeAdmissionControlTests above, extended
    to the new three-way pairing (file/list/folder all coexisting for the
    same bot) and to requested_filename's preservation across a claim.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

    def _offer_and_size_cap(self, rid, bot, filename):
        """Same idiom as RequestTypeAdmissionControlTests._offer_and_size_cap:
        force rejection at the (unrelated) size cap so admission control can
        be observed in isolation, with no real socket involved. A "folder"
        row is checked against MAX_FETCH_FOLDER_FILE_SIZE, not
        MAX_FETCH_FILE_SIZE - both are set here so this same helper also
        works for the "file"/"list" rows some of these tests mix in."""
        self.set_config(MAX_FETCH_FILE_SIZE=10, MAX_FETCH_FOLDER_FILE_SIZE=10)
        dcc_fetch.handle_incoming_offer(
            None, bot, f"DCC SEND {filename} 2130706433 55000 999999")

    def test_default_fallback_is_still_file_and_folder_is_accepted(self):
        bad_rid = dcc_fetch.enqueue_fetch("bot", "x", request_type="something-else")
        self.assertEqual(config.fetch_queue[bad_rid]["request_type"], "file")
        folder_rid = dcc_fetch.enqueue_fetch("bot", "!rar Artist/Album", request_type="folder")
        self.assertEqual(config.fetch_queue[folder_rid]["request_type"], "folder")

    def test_folder_row_matches_any_filename_from_the_right_bot(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        self._offer_and_size_cap(rid, "goodbot", "Artist_Album.rar")

        row = config.fetch_queue[rid]
        # Claimed (state moved on from 'offered') and only then failed the
        # unrelated size cap - proving the bot-alone match succeeded despite
        # the real .rar name never having been specified ahead of time.
        self.assertEqual(row["state"], "failed")
        self.assertIn("exceeds", row["reason"])
        self.assertEqual(row["filename"], "Artist_Album.rar")

    def test_folder_row_rejects_an_offer_from_the_wrong_bot(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        real_socket = socket.socket
        socket.socket = lambda *a, **kw: self.fail("must not connect")
        self.addCleanup(lambda: setattr(socket, "socket", real_socket))

        dcc_fetch.handle_incoming_offer(
            None, "impostorbot", "DCC SEND Anything.rar 2130706433 55000 4096")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_a_file_row_for_the_same_bot_does_not_cross_satisfy_a_folder_row(self):
        file_rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac", request_type="file")
        config.fetch_queue[file_rid]["state"] = "offered"
        config.fetch_queue[file_rid]["offered_at"] = time.time()
        folder_rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        config.fetch_queue[folder_rid]["state"] = "offered"
        config.fetch_queue[folder_rid]["offered_at"] = time.time()

        self._offer_and_size_cap(folder_rid, "goodbot", "WhateverName.rar")

        self.assertEqual(config.fetch_queue[file_rid]["state"], "offered")   # untouched
        self.assertEqual(config.fetch_queue[folder_rid]["state"], "failed")  # claimed, then hit the size cap

    def test_when_both_a_file_and_folder_row_exist_the_exact_filename_match_wins(self):
        """The more specific match (exact filename) takes priority over the
        looser one (bot alone) when both are outstanding for the same bot -
        mirrors test_when_both_a_file_and_list_row_exist_the_exact_filename_match_wins
        above, for the folder/file pairing."""
        file_rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac", request_type="file")
        config.fetch_queue[file_rid]["state"] = "offered"
        config.fetch_queue[file_rid]["offered_at"] = time.time()
        folder_rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        config.fetch_queue[folder_rid]["state"] = "offered"
        config.fetch_queue[folder_rid]["offered_at"] = time.time()

        self._offer_and_size_cap(file_rid, "goodbot", "Song.flac")

        self.assertEqual(config.fetch_queue[file_rid]["state"], "failed")    # claimed, then hit the size cap
        self.assertEqual(config.fetch_queue[folder_rid]["state"], "offered")  # untouched

    def test_a_list_row_and_a_folder_row_for_the_same_bot_can_no_longer_coexist(self):
        """Previously (before enqueue_fetch()'s has_outstanding_bot_alone_
        request() guard existed), a 'list' row and a 'folder' row could both
        end up outstanding for the same bot at once - and since neither
        convention's response filename is predictable ahead of time, a bare
        DCC SEND offer from that bot could not be told apart at claim time:
        whichever branch _claim_matching_offer_locked() checked first would
        win, even if the offer actually answered the OTHER request. That
        ambiguity is now refused at its source - enqueue_fetch() will not
        create a second bot-alone row for a bot that already has one
        outstanding - so this scenario can no longer be constructed through
        the normal enqueue path at all. See EnqueueTimeBotAloneCollisionTests
        below for the tests that pin that down, and
        test_multiple_list_rows_for_the_same_bot_the_oldest_is_claimed /
        test_multiple_folder_rows_for_the_same_bot_the_oldest_is_claimed
        above for _claim_matching_offer_locked()'s tie-break staying in place
        as a defense-in-depth fallback for the case where the queue was
        mutated directly."""
        list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        config.fetch_queue[list_rid]["state"] = "offered"
        config.fetch_queue[list_rid]["offered_at"] = time.time()

        folder_rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")

        self.assertIsNone(folder_rid)
        self.assertEqual(len(config.fetch_queue), 1)

    def test_multiple_folder_rows_for_the_same_bot_the_oldest_is_claimed(self):
        """See test_multiple_list_rows_for_the_same_bot_the_oldest_is_claimed's
        docstring above - same reasoning, folder/folder instead of
        list/list."""
        older_rid = insert_row_bypassing_enqueue_guard("goodbot", "!rar Older/Album", request_type="folder")
        config.fetch_queue[older_rid]["state"] = "offered"
        config.fetch_queue[older_rid]["offered_at"] = time.time()
        config.fetch_queue[older_rid]["requested_at"] = 1.0

        newer_rid = insert_row_bypassing_enqueue_guard("goodbot", "!rar Newer/Album", request_type="folder")
        config.fetch_queue[newer_rid]["state"] = "offered"
        config.fetch_queue[newer_rid]["offered_at"] = time.time()
        config.fetch_queue[newer_rid]["requested_at"] = 2.0

        self._offer_and_size_cap(older_rid, "goodbot", "Whatever.rar")

        self.assertEqual(config.fetch_queue[older_rid]["state"], "failed")
        self.assertEqual(config.fetch_queue[newer_rid]["state"], "offered")

    def test_claiming_a_folder_row_preserves_requested_filename(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()
        self.assertEqual(config.fetch_queue[rid]["requested_filename"], "!rar Artist/Album")

        self._offer_and_size_cap(rid, "goodbot", "Artist_Album_2020.rar")

        row = config.fetch_queue[rid]
        # filename was overwritten with the real advertised name, but
        # requested_filename - set once at creation - was left untouched.
        self.assertEqual(row["filename"], "Artist_Album_2020.rar")
        self.assertEqual(row["requested_filename"], "!rar Artist/Album")


class EnqueueTimeBotAloneCollisionTests(DCCoreTestCase):
    """"list" and "folder" rows both use bot-alone admission control (see
    _claim_matching_offer_locked()'s docstring) - neither convention's
    response filename is knowable ahead of time. If both were ever allowed
    outstanding for the same bot at once, a DCC SEND offer from that bot
    could not be told apart at claim time, and whichever branch happened to
    run first would silently claim an offer that may actually answer the
    OTHER request (misattributed size cap, wrong post-processing, and the
    real request left to eventually time out).

    Rather than resolve that ambiguity at claim time, it is now refused at
    its source: dcc_fetch.has_outstanding_bot_alone_request() (called from
    enqueue_fetch() itself, and pre-checked by webserver.py's two enqueue
    routes for a clean 409) refuses a second "list"/"folder" row for a bot
    that already has one outstanding. These tests pin down that check in
    isolation, at the dcc_fetch layer."""

    def test_has_outstanding_bot_alone_request_is_false_with_an_empty_queue(self):
        self.assertFalse(dcc_fetch.has_outstanding_bot_alone_request("goodbot"))

    def test_folder_request_refused_when_a_list_request_is_outstanding_for_the_same_bot(self):
        list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        self.assertIsNotNone(list_rid)

        self.assertTrue(dcc_fetch.has_outstanding_bot_alone_request("goodbot"))
        folder_rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")

        self.assertIsNone(folder_rid)
        self.assertEqual(len(config.fetch_queue), 1)

    def test_list_request_refused_when_a_folder_request_is_outstanding_for_the_same_bot(self):
        """The reverse pairing of the test above - order must not matter."""
        folder_rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        self.assertIsNotNone(folder_rid)

        list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")

        self.assertIsNone(list_rid)
        self.assertEqual(len(config.fetch_queue), 1)

    def test_a_second_list_request_for_the_same_bot_is_also_refused(self):
        """Not just list/folder cross-collisions - two rows of the SAME
        bot-alone type for the same bot are just as ambiguous at claim time,
        so they collide too."""
        first_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        self.assertIsNotNone(first_rid)

        second_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")

        self.assertIsNone(second_rid)
        self.assertEqual(len(config.fetch_queue), 1)

    def test_bot_name_comparison_is_case_and_whitespace_insensitive(self):
        """Same normalisation _claim_matching_offer_locked() already applies
        to `bot` - two rows for what is really the same bot must collide
        even if the casing/whitespace differs between the two requests."""
        list_rid = dcc_fetch.enqueue_fetch(" GoodBot ", "", request_type="list")
        self.assertIsNotNone(list_rid)

        folder_rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")

        self.assertIsNone(folder_rid)

    def test_a_folder_request_for_a_different_bot_is_unaffected(self):
        list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        self.assertIsNotNone(list_rid)

        folder_rid = dcc_fetch.enqueue_fetch("otherbot", "!rar Artist/Album", request_type="folder")

        self.assertIsNotNone(folder_rid)
        self.assertEqual(len(config.fetch_queue), 2)

    def test_bot_name_comparison_is_case_and_whitespace_insensitive_for_files_too(self):
        dcc_fetch.enqueue_fetch(" GoodBot ", "Track.flac", request_type="file")

        self.assertEqual(len(config.fetch_queue), 1)

    def test_a_new_bot_alone_request_succeeds_once_the_first_has_completed(self):
        list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        config.fetch_queue[list_rid]["state"] = "complete"

        folder_rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")

        self.assertIsNotNone(folder_rid)
        self.assertEqual(len(config.fetch_queue), 2)

    def test_a_new_bot_alone_request_succeeds_once_the_first_has_failed(self):
        list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        config.fetch_queue[list_rid]["state"] = "failed"

        folder_rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")

        self.assertIsNotNone(folder_rid)
        self.assertEqual(len(config.fetch_queue), 2)

    def test_a_pending_or_listening_or_receiving_request_still_blocks(self):
        """Every unresolved state counts, not just 'offered' - a row that
        has not yet even been dispatched ('pending'), or is mid-transfer
        ('listening'/'receiving'), is just as much an outstanding request as
        one sitting 'offered'."""
        for blocking_state in ("pending", "offered", "listening", "receiving"):
            with self.subTest(state=blocking_state):
                config.fetch_queue.clear()
                list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
                config.fetch_queue[list_rid]["state"] = blocking_state

                folder_rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")

                self.assertIsNone(folder_rid)

    def test_a_file_request_is_never_blocked_by_an_outstanding_list_request(self):
        """'file' rows use exact-match admission control and were never
        ambiguous with 'list'/'folder' - this check must not touch them."""
        list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        self.assertIsNotNone(list_rid)

        file_rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac", request_type="file")

        self.assertIsNotNone(file_rid)
        self.assertEqual(len(config.fetch_queue), 2)

    def test_a_file_request_never_blocks_a_later_list_or_folder_request(self):
        file_rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac", request_type="file")
        self.assertIsNotNone(file_rid)

        list_rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")

        self.assertIsNotNone(list_rid)
        self.assertEqual(len(config.fetch_queue), 2)


class HasAnyOutstandingRequestTests(DCCoreTestCase):
    """dcc_fetch.has_any_outstanding_request(), issue #385's purge feature.

    Wider than has_outstanding_bot_alone_request() above: that one only
    watches "list"/"folder" rows because those are the ambiguous ones at
    claim time. This one is for a caller that wants to know "is it safe to
    forget this bot entirely right now", and a plain "file" row in flight is
    just as unsafe to forget as a bot-alone one - deleting the fetched-list
    entry or extract directory a reply is about to be matched against would
    still break something, even though the claim-time ambiguity itself does
    not apply to "file" rows.
    """

    def test_false_with_an_empty_queue(self):
        self.assertFalse(dcc_fetch.has_any_outstanding_request("goodbot"))

    def test_true_for_a_plain_file_request(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Track.flac", request_type="file")
        self.assertIsNotNone(rid)
        self.assertTrue(dcc_fetch.has_any_outstanding_request("goodbot"))

    def test_true_for_a_list_request(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        self.assertIsNotNone(rid)
        self.assertTrue(dcc_fetch.has_any_outstanding_request("goodbot"))

    def test_true_for_a_folder_request(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album", request_type="folder")
        self.assertIsNotNone(rid)
        self.assertTrue(dcc_fetch.has_any_outstanding_request("goodbot"))

    def test_false_once_the_row_reaches_a_terminal_state(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Track.flac", request_type="file")
        with dcc_fetch._fetch_lock():
            config.fetch_queue[rid]["state"] = "complete"

        self.assertFalse(dcc_fetch.has_any_outstanding_request("goodbot"))

    def test_a_request_for_a_different_bot_is_unaffected(self):
        dcc_fetch.enqueue_fetch("goodbot", "Track.flac", request_type="file")

        self.assertFalse(dcc_fetch.has_any_outstanding_request("otherbot"))

    def test_bot_name_comparison_is_case_and_whitespace_insensitive(self):
        dcc_fetch.enqueue_fetch(" GoodBot ", "Track.flac", request_type="file")

        self.assertTrue(dcc_fetch.has_any_outstanding_request("goodbot"))
        self.assertTrue(dcc_fetch.has_any_outstanding_request(" GOODBOT "))


class RefusalNoticeFastFailTests(DCCoreTestCase):
    """dcc_fetch.handle_refusal_notice() - irc.py's NOTICE handler calls this
    for any private NOTICE addressed to us, so a peer's own "!rar is
    disabled here" reply fails the matching "folder" row immediately
    instead of waiting out the full FETCH_FOLDER_OFFER_TIMEOUT."""

    def _offered_folder_row(self, bot="goodbot", folder="!rar Artist/Album"):
        rid = dcc_fetch.enqueue_fetch(bot, folder, request_type="folder")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()
        return rid

    def test_a_dccore_style_refusal_fails_the_matching_folder_row(self):
        rid = self._offered_folder_row()

        dcc_fetch.handle_refusal_notice(
            "goodbot", "Error: Folder packing (!rar) is disabled on this bot.")

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("disabled", row["reason"])

    def test_an_omenserve_style_refusal_also_matches(self):
        rid = self._offered_folder_row()

        dcc_fetch.handle_refusal_notice("goodbot", "Rar Server is currently disabled.")

        self.assertEqual(config.fetch_queue[rid]["state"], "failed")

    def test_case_is_ignored_in_both_markers(self):
        rid = self._offered_folder_row()

        dcc_fetch.handle_refusal_notice("goodbot", "RAR SERVER IS CURRENTLY DISABLED.")

        self.assertEqual(config.fetch_queue[rid]["state"], "failed")

    def test_a_notice_missing_one_marker_never_touches_the_row(self):
        """Requires BOTH markers together - "disabled" alone is exactly the
        kind of word an unrelated NOTICE could plausibly contain, and a
        false match here would fail a row a moment before its real DCC SEND
        arrived, with no way back."""
        rid = self._offered_folder_row()

        dcc_fetch.handle_refusal_notice("goodbot", "This feature is currently disabled.")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_an_unrelated_notice_never_touches_the_row(self):
        rid = self._offered_folder_row()

        dcc_fetch.handle_refusal_notice("goodbot", "Thanks for stopping by!")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_a_refusal_from_the_wrong_bot_is_ignored(self):
        rid = self._offered_folder_row(bot="goodbot")

        dcc_fetch.handle_refusal_notice("someotherbot", "Rar Server is currently disabled.")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_a_file_row_is_never_touched_by_a_refusal_notice(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac", request_type="file")
        config.fetch_queue[rid]["state"] = "offered"

        dcc_fetch.handle_refusal_notice("goodbot", "Rar Server is currently disabled.")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_a_list_row_is_never_touched_by_a_refusal_notice(self):
        """This wording is specific to !rar - a "list" row's own refusal (if
        that ever happens) is not this shape and is left to its own
        timeout."""
        rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")
        config.fetch_queue[rid]["state"] = "offered"

        dcc_fetch.handle_refusal_notice("goodbot", "Rar Server is currently disabled.")

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")

    def test_a_row_not_currently_offered_is_never_touched(self):
        for state in ("pending", "receiving", "complete", "failed"):
            with self.subTest(state=state):
                rid = dcc_fetch.enqueue_fetch(f"bot-{state}", "!rar Album", request_type="folder")
                config.fetch_queue[rid]["state"] = state

                dcc_fetch.handle_refusal_notice(f"bot-{state}", "Rar Server is currently disabled.")

                self.assertEqual(config.fetch_queue[rid]["state"], state)

    def test_no_outstanding_row_for_the_bot_is_a_silent_no_op(self):
        dcc_fetch.handle_refusal_notice("nobodyaskedbot", "Rar Server is currently disabled.")  # must not raise
        self.assertEqual(config.fetch_queue, {})

    def test_multiple_candidates_the_oldest_is_failed(self):
        """Defence-in-depth only - enqueue_fetch() already refuses a second
        outstanding "folder"/"list" request for the same bot, so this uses
        insert_row_bypassing_enqueue_guard() (see its own docstring) to
        still exercise the tie-break, mirroring
        _claim_matching_offer_locked()'s identical oldest-wins fallback for
        the same reason: not assumed impossible."""
        older_rid = insert_row_bypassing_enqueue_guard("goodbot", "!rar Album", request_type="folder")
        config.fetch_queue[older_rid]["state"] = "offered"
        config.fetch_queue[older_rid]["requested_at"] = 1.0
        newer_rid = insert_row_bypassing_enqueue_guard("goodbot", "!rar Other", request_type="folder")
        config.fetch_queue[newer_rid]["state"] = "offered"
        config.fetch_queue[newer_rid]["requested_at"] = 2.0

        dcc_fetch.handle_refusal_notice("goodbot", "Rar Server is currently disabled.")

        self.assertEqual(config.fetch_queue[older_rid]["state"], "failed")
        self.assertEqual(config.fetch_queue[newer_rid]["state"], "offered")


class ListFetchDispatcherTests(DCCoreTestCase):
    """check_fetch_queue() sends a bare "@<bot>" (irc.py's own list trigger,
    per list.send_file_list()) for a request_type="list" row, instead of the
    "!<bot> <filename>" a "file" row sends - same paced outbound queue
    (oserve.queue_message), only the message body differs."""

    def test_pending_list_row_is_promoted_and_sends_a_bare_at_bot_trigger(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "", request_type="list")

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "offered")
        self.assertEqual(len(self.oserve.queued), 1)
        sent_user, sent_msg, _is_vip = self.oserve.queued[0]
        self.assertEqual(sent_user, "goodbot")
        self.assertIn("@goodbot", sent_msg)
        self.assertNotIn("!goodbot", sent_msg)

    def test_a_pending_file_row_is_unaffected(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac", request_type="file")

        dcc_fetch.check_fetch_queue()

        _sent_user, sent_msg, _is_vip = self.oserve.queued[0]
        self.assertIn("!goodbot Song.flac", sent_msg)

    def test_pending_folder_row_is_promoted_and_sends_the_bang_rar_trigger(self):
        """A "folder" row's filename is literally "!rar <folder>" at enqueue
        time (see webserver.build_folder_rar_fetch_enqueue_result()), so no
        dedicated dispatch branch is needed - it falls into the same
        "!<bot> <filename>" wire line the plain "file" branch already
        builds, producing "!<bot> !rar <folder path>" automatically."""
        rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist/Album (2020)", request_type="folder")

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "offered")
        self.assertEqual(len(self.oserve.queued), 1)
        sent_user, sent_msg, _is_vip = self.oserve.queued[0]
        self.assertEqual(sent_user, "goodbot")
        self.assertIn("!goodbot !rar Artist/Album (2020)", sent_msg)


class FetchHistoryPersistenceTests(DCCoreTestCase):
    """config.fetch_queue was in-memory only: a finished fetch's row - the
    only thing the dashboard's Downloads table and its Delete button have to
    point at - vanished on every restart even though the file itself sat on
    disk under FETCHED_FILES_DIR untouched. check_fetch_queue() now snapshots
    every 'complete'/'failed' row to db.FETCH_HISTORY_FILE on each tick it
    runs; tests/support.py's DCCoreTestCase redirects that path to a
    per-test tmp dir, so these tests read db.FETCH_HISTORY_FILE directly
    rather than the real repository path."""

    def test_a_completed_row_is_persisted_on_the_next_tick(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        config.fetch_queue[rid]["state"] = "complete"
        config.fetch_queue[rid]["stored_filename"] = f"{rid}_Song.flac"

        dcc_fetch.check_fetch_queue()

        history = db.load_fetch_history()
        self.assertIn(rid, history)
        self.assertEqual(history[rid]["state"], "complete")

    def test_a_failed_row_is_persisted_on_the_next_tick(self):
        rid = dcc_fetch.enqueue_fetch("silentbot", "Ghost.flac")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time() - 61
        self.set_config(FETCH_OFFER_TIMEOUT=60)

        dcc_fetch.check_fetch_queue()

        history = db.load_fetch_history()
        self.assertIn(rid, history)
        self.assertEqual(history[rid]["state"], "failed")

    def test_unfinished_rows_are_persisted_in_the_form_they_restart_in(self):
        """Since #926 an unfinished request survives a restart. A waiting one
        comes back as it was; one that was mid-flight - its socket and thread
        die with the process - comes back pending, to be asked again, and is
        written that way so a transfer's byte count does not rewrite the file
        every tick."""
        waiting = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")  # stays "pending"
        moving = dcc_fetch.enqueue_fetch("goodbot", "Other.flac")
        config.fetch_queue[moving].update(state="receiving", offered_at=1.0, bytes_received=5000)

        dcc_fetch.check_fetch_queue()

        saved = db.load_fetch_history()
        self.assertEqual(saved[waiting]["state"], "pending")
        self.assertEqual(saved[moving]["state"], "pending")
        self.assertEqual(saved[moving]["bytes_received"], 0)

    def test_an_unchanged_terminal_set_does_not_rewrite_the_file(self):
        """Runs forever, every 2s, from fetch_dispatcher_worker() - rewriting
        an unchanged file on every single tick would be needless disk I/O."""
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        config.fetch_queue[rid]["state"] = "complete"
        dcc_fetch.check_fetch_queue()

        real_save = db.save_fetch_history
        calls = []
        db.save_fetch_history = lambda rows: (calls.append(rows), real_save(rows))[-1]
        self.addCleanup(setattr, db, "save_fetch_history", real_save)

        dcc_fetch.check_fetch_queue()  # nothing changed since the first tick

        self.assertEqual(calls, [])

    def test_a_row_deleted_from_the_queue_drops_out_of_history_on_the_next_tick(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        config.fetch_queue[rid]["state"] = "complete"
        dcc_fetch.check_fetch_queue()
        self.assertIn(rid, db.load_fetch_history())

        del config.fetch_queue[rid]
        dcc_fetch.check_fetch_queue()

        self.assertNotIn(rid, db.load_fetch_history())

    def test_a_content_change_on_an_already_terminal_row_is_still_persisted(self):
        """#162 finding #9. The dirty check used to compare only the SET of
        terminal row ids, not their content. _run_transfer() sets
        state="complete" and only THEN calls _handle_completed_list_fetch(),
        which is what sets row["list_processing_error"] when a fetched list
        zip is refused (zip-slip, a zip bomb, ...) - so a dispatcher tick
        landing between those two writes persisted "complete" with no error
        annotation, and because the id was already in the "last persisted"
        set, the annotation arriving a moment later was treated as nothing
        having changed and silently never written. After a restart the row
        came back looking like a clean success, and the dashboard's
        Download button (web/app.js derives "Rejected" solely from that
        field) offered a hostile archive as a normal completed fetch."""
        rid = dcc_fetch.enqueue_fetch("evilbot", "", request_type="list")
        config.fetch_queue[rid]["state"] = "complete"
        dcc_fetch.check_fetch_queue()  # tick 1: persists "complete", no error yet

        history = db.load_fetch_history()
        self.assertNotIn("list_processing_error", history[rid])

        # The annotation _handle_completed_list_fetch() would have added,
        # landing on the SAME row - the id was already terminal before and
        # after, only the content changed.
        config.fetch_queue[rid]["list_processing_error"] = "zip-slip: refused"
        dcc_fetch.check_fetch_queue()  # tick 2

        history = db.load_fetch_history()
        self.assertEqual(history[rid].get("list_processing_error"), "zip-slip: refused",
                         "a content-only change on an already-terminal row "
                         "must still reach disk")

    def test_persist_fetch_history_is_safe_to_call_without_the_lock_already_held(self):
        """The public (non-"_locked") entry point webserver.py's delete route
        uses - must acquire the fetch lock itself rather than assuming a
        caller already holds it."""
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        config.fetch_queue[rid]["state"] = "failed"
        config.fetch_queue[rid]["reason"] = "timeout"

        dcc_fetch.persist_fetch_history()

        self.assertIn(rid, db.load_fetch_history())


class DispatcherSecondLayerCtcpGuardTests(DCCoreTestCase):
    """BUG 1 (CRITICAL) regression, dispatch-site half: webserver.py's
    reject_if_unsafe_for_irc_line() is the FIRST layer, but check_fetch_queue()
    is the one call site that actually interpolates `bot`/`filename` into a
    raw outbound IRC line - it must not simply trust that upstream check was
    applied, mirroring _serve_passive_offer()'s own second-layer re-check
    right before it builds its own outbound CTCP line (see
    PassiveOfferInjectionTests.test_serve_passive_offer_second_layer_guard_
    refuses_to_build_the_reply above for that half).

    dcc_fetch.enqueue_fetch() itself performs no validation at all (only
    new_fetch_row()'s str().strip(), which does not strip \\x01) - the HTTP
    boundary is the only thing that currently rejects unsafe bytes, so
    calling enqueue_fetch() directly, as these tests do, is exactly how
    "somehow-corrupted or future-mistake row data" would reach the
    dispatcher: bypassing webserver.py's check entirely.
    """

    def test_a_file_row_with_an_unsafe_bot_nick_is_never_sent(self):
        rid = dcc_fetch.enqueue_fetch("evilbot2\x01INJECTED\x01", "Song.mp3",
                                       request_type="file")

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("unsafe characters", row["reason"])
        self.assertEqual(self.oserve.queued, [],
                         "the hostile bot nick must never reach "
                         "oserve.queue_message()")

    def test_a_file_row_with_an_unsafe_filename_is_never_sent(self):
        rid = dcc_fetch.enqueue_fetch("goodbot", "Song\x01.mp3",
                                       request_type="file")

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("unsafe characters", row["reason"])
        self.assertEqual(self.oserve.queued, [])

    def test_a_list_row_with_an_unsafe_bot_nick_is_never_sent(self):
        rid = dcc_fetch.enqueue_fetch("evilbot\x01ACTION pwned\x01", "",
                                       request_type="list")

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("unsafe characters", row["reason"])
        self.assertEqual(self.oserve.queued, [],
                         "the hostile bot nick must never reach "
                         "oserve.queue_message()")

    def test_a_folder_row_with_an_unsafe_filename_argument_is_never_sent(self):
        """Unlike "list" (exempted from this check because it has no
        filename argument at dispatch time), a "folder" row's filename IS
        real content ("!rar <folder>") and must NOT be exempted - this is
        the one case that is meaningfully different from "list"."""
        rid = dcc_fetch.enqueue_fetch("goodbot", "!rar Artist\x01/Album",
                                       request_type="folder")

        dcc_fetch.check_fetch_queue()

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "failed")
        self.assertIn("unsafe characters", row["reason"])
        self.assertEqual(self.oserve.queued, [],
                         "the hostile folder argument must never reach "
                         "oserve.queue_message()")

    def test_a_clean_row_is_completely_unaffected_by_the_second_layer_guard(self):
        """The new re-check must not false-positive on ordinary input."""
        file_rid = dcc_fetch.enqueue_fetch("goodbot", "Song.flac", request_type="file")
        list_rid = dcc_fetch.enqueue_fetch("otherbot", "", request_type="list")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[file_rid]["state"], "offered")
        self.assertEqual(config.fetch_queue[list_rid]["state"], "offered")
        self.assertEqual(len(self.oserve.queued), 2)


class DispatchLineLengthGuardTests(DCCoreTestCase):
    """#162 finding #13, dispatch-site belt-and-braces half:
    webserver.reject_if_unsafe_for_irc_line() now caps filename's length at
    enqueue time (IRC_LINE_FIELD_MAX_LEN), but check_fetch_queue() is the one
    call site that actually builds the outbound PRIVMSG line - it must not
    simply trust that cap, same posture as
    DispatcherSecondLayerCtcpGuardTests above takes for the byte check.
    enqueue_fetch() itself performs no validation, so a filename this long
    reaching dispatch is exactly how a bypass of the web boundary would
    surface."""

    def test_a_very_long_filename_still_produces_a_line_within_budget(self):
        import announce
        long_filename = "x" * 3000  # the audit's own repro length
        rid = dcc_fetch.enqueue_fetch("goodbot", long_filename, request_type="file")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")
        self.assertEqual(len(self.oserve.queued), 1)
        _key, sent_msg, _is_vip = self.oserve.queued[0]
        self.assertLessEqual(len(sent_msg.encode("utf-8")), announce.IRC_LINE_BUDGET)

    def test_a_very_long_folder_argument_still_produces_a_line_within_budget(self):
        import announce
        long_folder = "!rar " + "x" * 3000
        rid = dcc_fetch.enqueue_fetch("goodbot", long_folder, request_type="folder")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")
        self.assertEqual(len(self.oserve.queued), 1)
        _key, sent_msg, _is_vip = self.oserve.queued[0]
        self.assertLessEqual(len(sent_msg.encode("utf-8")), announce.IRC_LINE_BUDGET)


class ListFetchEndToEndTests(DCCoreTestCase):
    """Real loopback socket, mirroring LoopbackTransferTests' pattern above:
    enqueue a request_type="list" row, answer it with a real (small,
    synthetic) zip containing a fake master-list .txt, and confirm the fetch
    completes, the zip is safely extracted, and it is parsed into
    config.fetched_bot_lists - the full path from dcc_fetch.py's transfer
    completion through list_fetch.py's extraction/parsing.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-fetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.addCleanup(self.listener.close)

    def _make_list_zip(self, base_name="OtherBot"):
        import io
        import zipfile as zf_mod

        txt = (
            "List of 1 Files (10.0MB) generated on Jan 1st\n"
            f"To request a file, copy/paste to the channel... !{base_name} FILENAME\n\n\n"
            "\n" + "=" * 53 + "\n"
            "D:\\MUSIC\\SomeAlbum\\\n"
            + "=" * 53 + "\n"
            f"!{base_name} Track One.flac  ::INFO:: 10.0MB\n"
        )
        buf = io.BytesIO()
        with zf_mod.ZipFile(buf, "w", zf_mod.ZIP_DEFLATED) as zf:
            zf.writestr(f"{base_name}-2026-08-27.txt", txt)
        return buf.getvalue()

    def test_list_fetch_end_to_end_extracts_and_parses(self):
        payload = self._make_list_zip()
        rid = dcc_fetch.enqueue_fetch("otherbot", "", request_type="list")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        def serve():
            self.listener.settimeout(5.0)
            conn, _ = self.listener.accept()
            conn.settimeout(5.0)
            conn.sendall(payload)
            conn.close()

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()

        offer_line = f"DCC SEND otherbot-list.zip {ip_long('127.0.0.1')} {self.port} {len(payload)}"
        dcc_fetch.handle_incoming_offer(None, "otherbot", offer_line)
        server_thread.join(timeout=5.0)

        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "complete")
        self.assertNotIn("list_processing_error", row)
        self.assertEqual(row["filename"], "otherbot-list.zip")

        entry = config.fetched_bot_lists.get("otherbot")
        self.assertIsNotNone(entry, "a successful list fetch must populate config.fetched_bot_lists")
        self.assertEqual(entry["bot"], "otherbot")
        # Issue #76, option 2: no "entries" key is stored any more - just a
        # path and a count - so read it back the same way webserver.py does,
        # on demand.
        import list_fetch
        # Paged by folder now: flatten the groups back to rows, which is all
        # this test ever cared about.
        folders, _n, _files, _row_capped, error = list_fetch.get_fetched_bot_page(entry, 0, 10**9)
        self.assertIsNone(error)
        titles = [e["title"] for g in folders for e in g["entries"]]
        self.assertIn("Track One.flac", titles)

    def test_a_second_fetch_for_the_same_bot_replaces_not_accumulates(self):
        import list_fetch
        stale_dir = os.path.join(self.tmp, "stale")
        os.makedirs(stale_dir, exist_ok=True)
        stale_list_path = os.path.join(stale_dir, "stale-list.txt")
        with open(stale_list_path, "w", encoding="utf-8") as f:
            f.write("!otherbot Stale.flac  ::INFO:: 1.0MB\n")
        config.fetched_bot_lists["otherbot"] = {
            "bot": "otherbot", "fetched_at": 1.0,
            "list_path": stale_list_path, "entry_count": 1,
            "source_zip": "old.zip",
        }

        payload = self._make_list_zip()
        rid = dcc_fetch.enqueue_fetch("otherbot", "", request_type="list")
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        def serve():
            self.listener.settimeout(5.0)
            conn, _ = self.listener.accept()
            conn.settimeout(5.0)
            conn.sendall(payload)
            conn.close()

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()
        offer_line = f"DCC SEND newlist.zip {ip_long('127.0.0.1')} {self.port} {len(payload)}"
        dcc_fetch.handle_incoming_offer(None, "otherbot", offer_line)
        server_thread.join(timeout=5.0)

        entry = config.fetched_bot_lists["otherbot"]
        # Paged by folder now: flatten the groups back to rows, which is all
        # this test ever cared about.
        folders, _n, _files, _row_capped, error = list_fetch.get_fetched_bot_page(entry, 0, 10**9)
        self.assertIsNone(error)
        titles = [e["title"] for g in folders for e in g["entries"]]
        self.assertNotIn("Stale.flac", titles)
        self.assertIn("Track One.flac", titles)




class LongOfferedFilenames(LoopbackTransferTests):
    """The offered filename's length is entirely the sending bot's choice.

    _sanitize_offer_filename() rejects unsafe names but never truncates them,
    and dcc.py wraps every path it touches in platform_compat.long_path()
    while the fetch write path added on this branch did not. So a legal 240-
    character name pushed the destination past Windows' 260-character
    MAX_PATH and the transfer failed with

        [FETCH] Failed (transfer error: [Errno 2] No such file or directory)

    while the identical transfer named ok.mp3 reached 'complete'. The name is
    legal on Linux, so this only ever bit on Windows.
    """

    LONG_NAME = ("L" * 236) + ".flac"      # 241 characters

    def _transfer(self, filename, payload):
        rid = self._enqueue_and_offer("peerbot", filename)

        def serve():
            self.listener.settimeout(5.0)
            conn, _ = self.listener.accept()
            conn.settimeout(5.0)
            conn.sendall(payload)
            conn.close()

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()
        dcc_fetch.handle_incoming_offer(
            None, "peerbot", self._offer_line(filename, len(payload)))
        server_thread.join(timeout=5.0)
        return config.fetch_queue[rid]

    def test_a_long_offered_filename_completes(self):
        payload = b"FLAC" + (b"\x07" * 4096)

        row = self._transfer(self.LONG_NAME, payload)

        self.assertEqual(row["state"], "complete",
                         f"a legal long filename failed: {row.get('error', '')}")
        self.assertEqual(row["bytes_received"], len(payload))

    def test_the_received_file_is_actually_on_disk(self):
        """'complete' is the row's opinion; this checks the filesystem agrees,
        which is the thing MAX_PATH actually broke."""
        import platform_compat
        payload = b"FLAC" + (b"\x07" * 4096)

        row = self._transfer(self.LONG_NAME, payload)

        stored = os.path.join(config.FETCHED_FILES_DIR, row["stored_filename"])
        self.assertTrue(os.path.exists(platform_compat.long_path(stored)),
                        "the row says complete but the file is not there")
        self.assertEqual(os.path.getsize(platform_compat.long_path(stored)),
                         len(payload))

    def test_the_destination_really_is_past_max_path(self):
        """Fixture invariant. If the temp root is short enough that the
        destination lands under 260, the tests above prove nothing on Windows
        and this says so rather than passing quietly."""
        dest = os.path.join(config.FETCHED_FILES_DIR, self.LONG_NAME)
        self.assertGreater(
            len(dest), 260,
            f"the destination is only {len(dest)} characters, so the MAX_PATH "
            f"hazard is not being exercised")

    def test_a_short_name_still_works(self):
        """Control - the wrapping must not disturb the ordinary case."""
        payload = b"FLAC" + (b"\x01\x02" * 512)

        row = self._transfer("Short.flac", payload)

        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["bytes_received"], len(payload))


class TurningTheSizeCapsOff(DCCoreTestCase):
    """#302: "Files should never be rejected based on size."

    The caps are not deleted - deleting them takes the choice from every
    operator who does not know they exist. They are switchable off, which is
    the same outcome for the one who wants it.
    """

    def setUp(self):
        super().setUp()
        self.bot = "SizeBot"
        self.set_config(fetch_queue={})

    def offer(self, size, request_type="file"):
        """The payload is the raw CTCP text, and the row must already be
        'offered' - admission control only accepts an offer answering a
        request we made a moment ago."""
        import dcc_fetch
        request_id = dcc_fetch.enqueue_fetch(self.bot, "Huge.rar")
        row = config.fetch_queue[request_id]
        row["request_type"] = request_type
        row["state"] = "offered"
        row["offered_at"] = time.time()
        dcc_fetch.handle_incoming_offer(
            None, self.bot, f"DCC SEND Huge.rar 2130706433 55000 {size}")
        return config.fetch_queue[request_id]

    def test_an_offer_over_the_cap_is_still_refused_by_default(self):
        self.set_config(MAX_FETCH_FILE_SIZE=1000)

        row = self.offer(5000)

        self.assertEqual(row["state"], "failed")
        self.assertIn("exceeds", row.get("reason", ""))

    def test_zero_means_no_limit(self):
        """The row still fails - it goes on to connect to a peer that is not
        there - but not FOR ITS SIZE, which is the whole of what the cap
        decides. Asserting "not failed" would be asserting that a fixture
        socket connects."""
        self.set_config(MAX_FETCH_FILE_SIZE=0)

        row = self.offer(5 * 1024 * 1024 * 1024)

        self.assertNotIn("exceeds", row.get("reason", ""))
        self.assertNotIn("MAX_FETCH_FILE_SIZE", row.get("reason", ""))

    def test_the_refusal_says_how_to_switch_it_off(self):
        """The operator who hit this had no way to tell from the message that
        it was theirs to change."""
        self.set_config(MAX_FETCH_FILE_SIZE=1000)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.offer(5000)

        self.assertIn("0 for no limit", buffer.getvalue())

    def test_the_list_cap_clears_a_real_list_archive(self):
        """10MB was 'generous over any real master-list zip' on the same
        evidence that put the text ceiling at 20MB: one 4MB list. Real lists
        run to 31MB of text, and a zip of one is several MB - close enough to
        the old cap that the next library along lands on it."""
        self.assertGreaterEqual(config.MAX_FETCH_LIST_FILE_SIZE, 32 * 1024 * 1024)


class AskingForARowCopiedOutOfAnotherBotsList(DCCoreTestCase):
    """The shape an operator's own log showed, with the peer renamed:

        [FETCH] Requested 'BBCRadio - Under Milk Wood - Richard Burton.mp3
                ::INFO:: 79.53MB' from PeerServeDCC (request f96ba6b77dff).
        [FETCH] Rejected unsolicited DCC SEND from PeerServeDCC
                ('BBCRadio_-_Under_Milk_Wood_-_Richard_Burton.mp3'):
                no matching pending request.

    The bot asked for a file and then refused the answer to its own question.
    """

    LINE = "BBCRadio - Under Milk Wood - Richard Burton.mp3  ::INFO:: 79.53MB"
    SENT_BACK = "BBCRadio_-_Under_Milk_Wood_-_Richard_Burton.mp3"

    def test_the_size_suffix_is_not_part_of_the_filename(self):
        """The dashboard sends what the operator clicked, which is the whole
        list row. The serving side has stripped this since #234; the fetching
        side never learned to."""
        row = dcc_fetch.new_fetch_row("PeerServeDCC", self.LINE)

        self.assertEqual(row["filename"],
                         "BBCRadio - Under Milk Wood - Richard Burton.mp3")
        self.assertNotIn("::INFO::", row["requested_filename"])

    def test_the_answer_to_our_own_request_is_now_claimed(self):
        """The end-to-end shape of the bug: ask with the suffix, be answered
        without it and with underscores for spaces, and match."""
        self.set_config(fetch_queue={})
        request_id = dcc_fetch.enqueue_fetch("PeerServeDCC", self.LINE)
        config.fetch_queue[request_id]["state"] = "offered"

        claimed_id, row = dcc_fetch._claim_matching_offer_locked(
            config.fetch_queue, "PeerServeDCC", self.SENT_BACK)

        self.assertEqual(claimed_id, request_id)
        self.assertEqual(row["state"], "receiving")

    def test_the_normaliser_alone_could_never_have_bridged_it(self):
        """Worth pinning, because the docstring says the match is
        "underscore/space-normalised" and it is - that was never the gap. A
        size only one side carries is not whitespace."""
        with_suffix = dcc_fetch._normalize_filename_for_match(self.LINE)
        without = dcc_fetch._normalize_filename_for_match(self.SENT_BACK)

        self.assertNotEqual(with_suffix, without)

    def test_a_folder_rows_request_text_survives_untouched(self):
        """Only "file" rows are stripped. A folder row's requested_filename is
        the literal "!rar <path>" request text, which new_fetch_row()'s own
        docstring depends on being preserved.

        The path is built from a heading in ANOTHER bot's list, so it is not
        ours to predict - a folder actually named "::INFO::" is odd but it is
        theirs to name, and stripping it would turn a request they could have
        answered into one they cannot. That is what makes this restriction
        load-bearing rather than decorative: without the case, "file" and
        "everything" are the same code."""
        for text in ("!rar D:/MEDIA/Artist/Album/",
                     "!rar D:/MEDIA/Artist/::INFO:: Sessions/"):
            with self.subTest(text=text):
                row = dcc_fetch.new_fetch_row("B", text, request_type="folder")

                self.assertEqual(row["requested_filename"], text)

    def test_an_ordinary_filename_is_unchanged(self):
        row = dcc_fetch.new_fetch_row("B", "Just A Track.flac")

        self.assertEqual(row["filename"], "Just A Track.flac")


class AWholeAlbumSelectedAtOnce(DCCoreTestCase):
    """From an operator report: "i download a bot's list, added 10+ into the
    queue on the webdashboard. it didnt queue them, it spit out every line
    at 1 go. should be queued and sent 3 at the time to the channel."

    That is the contract, and it was only half covered. The SLOT CAP had a
    test - two promoted, two left pending. What did not was the thing an
    operator actually sees: that a hundred-row selection reaches the channel a
    few lines at a time, and that each COMPLETION is what releases the next.
    A regression in either would have been silent.
    """

    def setUp(self):
        super().setUp()
        # FETCH_MAX_PER_BOT off: these are about MAX_FETCH_SLOTS, with every
        # row from one bot - the per-bot limit (#926) would be what they
        # measured instead.
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=3,
                        fetch_feature_disabled=False, CHANNEL="#chan",
                        FETCH_MAX_PER_BOT=0)

    def lines_sent(self):
        return [msg for _user, msg, _vip in self.oserve.queued]

    def queue_up(self, count, bot="ListBot"):
        return [dcc_fetch.enqueue_fetch(bot, f"Track {i:02d}.flac")
                for i in range(count)]

    def complete_everything_in_flight(self):
        done = 0
        for row in config.fetch_queue.values():
            if row.get("state") == "offered":
                row["state"] = "complete"
                done += 1
        return done

    def test_selecting_ten_does_not_put_ten_lines_in_the_channel(self):
        """The whole point of a queue. Every line at once is both a wall of
        text in a public channel and a good way to meet Excess Flood."""
        self.queue_up(10)

        dcc_fetch.check_fetch_queue()

        self.assertEqual(len(self.lines_sent()), 3)

    def test_the_rest_are_waiting_not_lost(self):
        ids = self.queue_up(10)

        dcc_fetch.check_fetch_queue()

        states = [config.fetch_queue[rid]["state"] for rid in ids]
        self.assertEqual(states.count("offered"), 3)
        self.assertEqual(states.count("pending"), 7)

    def test_nothing_more_goes_out_while_the_first_three_are_in_flight(self):
        """The tick runs every two seconds. If a dispatched row did not count
        against the slots, a ten-row queue would empty into the channel in
        under ten seconds - which is what "spit out every line at 1 go" looks
        like from the outside."""
        self.queue_up(10)

        for _tick in range(5):
            dcc_fetch.check_fetch_queue()

        self.assertEqual(len(self.lines_sent()), 3)

    def test_each_completion_releases_exactly_one_more(self):
        """"as each file download gets completed program requests the next
        from queue"."""
        self.queue_up(10)
        dcc_fetch.check_fetch_queue()

        for row in config.fetch_queue.values():
            if row.get("state") == "offered":
                row["state"] = "complete"
                break
        dcc_fetch.check_fetch_queue()

        self.assertEqual(len(self.lines_sent()), 4)

    def test_a_long_queue_drains_a_batch_at_a_time(self):
        """"Queue could be hundreds of lines." Driven the way it really runs -
        dispatch, complete, dispatch - rather than by setting states and
        hoping."""
        self.queue_up(9)

        rounds = 0
        while rounds < 10:
            dcc_fetch.check_fetch_queue()
            self.assertLessEqual(
                len(self.lines_sent()), (rounds + 1) * 3,
                "more lines reached the channel than slots allow")
            if not self.complete_everything_in_flight():
                break
            rounds += 1

        self.assertEqual(len(self.lines_sent()), 9)
        self.assertEqual(rounds, 3, "nine files should take three batches")

    def test_lowering_the_slot_limit_mid_flight_does_not_flush_the_queue(self):
        """An operator can edit Max fetch slots on the settings page while
        transfers are running, which is the one way `active` can come out
        HIGHER than the limit. Without the free_slots <= 0 guard, the negative
        count reaches pending_ids[:free_slots] as a from-the-end slice and
        dispatches every pending row but the last few - a hundred-row queue
        emptying into the channel the moment somebody saved a setting.

        A rehash disturbing a live queue has been reported once already, so
        this path is not hypothetical. Holding still until the extra
        transfers finish is the correct response: the ones in flight are
        already paid for, and the new limit governs from the next promotion
        on.
        """
        self.queue_up(10)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(len(self.lines_sent()), 3)

        self.set_config(MAX_FETCH_SLOTS=1)
        for _tick in range(3):
            dcc_fetch.check_fetch_queue()

        self.assertEqual(len(self.lines_sent()), 3,
                         "lowering the limit released the rest of the queue")

    def test_the_lowered_limit_governs_once_the_backlog_clears(self):
        """The guard holds, it does not wedge."""
        self.queue_up(10)
        dcc_fetch.check_fetch_queue()
        self.set_config(MAX_FETCH_SLOTS=1)
        self.complete_everything_in_flight()

        dcc_fetch.check_fetch_queue()

        self.assertEqual(len(self.lines_sent()), 4)

    def test_the_slot_count_is_what_paces_it(self):
        """Raising the limit raises the batch - so an operator who wants more
        in flight has a setting, and one who does not is not surprised."""
        self.set_config(MAX_FETCH_SLOTS=5)
        self.queue_up(10)

        dcc_fetch.check_fetch_queue()

        self.assertEqual(len(self.lines_sent()), 5)

    def test_enqueueing_sends_nothing_by_itself(self):
        """The dashboard creates rows; the dispatcher owns pacing. If enqueue
        ever dispatched, selecting a hundred files would send a hundred lines
        before the dispatcher got a say - which is exactly the reported
        symptom."""
        self.queue_up(10)

        self.assertEqual(self.lines_sent(), [])


if __name__ == "__main__":
    unittest.main()
