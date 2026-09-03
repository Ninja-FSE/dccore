"""Regression tests for announce.py - IRC line safety and the debug pump.

Two families of defect are covered here.

LINE SAFETY. An IRC line is capped at 512 bytes including the CRLF, and the
server prepends ":nick!ident@host " before relaying it, which counts against
the same 512 for every recipient. The announcement templates in this module are
dense with mIRC colour codes, so when a long classical filename or a long
user-supplied @find term pushed a line over the cap the server cut it in the
middle of a colour code and the background colour smeared to the end of the
line in everybody's client. fit_irc_line() now re-renders the template with a
shrinking value - measured in BYTES, because an umlaut costs two - so the fixed
parts of the line, colour codes included, always survive intact.

THE DEBUG PUMP. send_debug used to write the socket directly with a blocking
time.sleep(0.5) held under a lock. security.check_user_status calls it from the
IRC READ THREAD once per denied PRIVMSG, so thirty banned nicks reconnecting
after a netsplit froze the network loop for fifteen seconds: no PONG, no NAMES,
no dispatch. It is now a bounded queue drained by one background thread, and
the HARDBAN category - raised for blocked path traversal - renders its own
[SECURITY] label instead of disappearing into the grey [INFO] chatter.

Every test's docstring names the defect it guards against.
"""

import contextlib
import io
import os
import tempfile
import time
import unittest

from tests.support import DCCoreTestCase, RecordingSocket, silence_debug

import announce

# announce.send_debug is a module global that other test modules replace with a
# capture. Remember the genuine implementation at import time so the cases that
# exercise the real pump cannot be poisoned by whatever ran before them.
_REAL_SEND_DEBUG = announce.send_debug

# A 158-character classical track name of the kind that actually lives on the
# NFS mount. This is the input that first blew the transfer-complete line past
# 512 bytes in production.
CLASSICAL_TRACK = (
    "Ludwig van Beethoven - Symphony No. 9 in D minor, Op. 125 'Choral' - "
    "IV. Finale - Presto - Allegro assai vivace - Alla marcia, Berliner (Karajan 1963 DG).flac"
)


def encoded_len(line):
    """Bytes on the wire, which is the only length the IRC server cares about."""
    return len(line.encode("utf-8", errors="ignore"))


class QuietTestCase(DCCoreTestCase):
    """Base case that swallows the daemon's prints.

    announce.py prints the search term and the filename to stdout. On a Windows
    console using a non-Latin code page (cp1253 here) printing a non-ASCII term
    raises UnicodeEncodeError, which would turn a passing test red for reasons
    that have nothing to do with the daemon. Capture stdout instead.
    """

    def setUp(self):
        super().setUp()
        self._stdout_sink = io.StringIO()
        redirect = contextlib.redirect_stdout(self._stdout_sink)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)


class FitIrcLineTests(QuietTestCase):
    """fit_irc_line must shrink the variable field, never the template."""

    # A stand-in for the real templates: a colour code up front, a colour-coded
    # fixed tail behind the variable field, and the CRLF the daemon always adds.
    HEAD = "PRIVMSG #dccore-test :\x0304,05 \x0310,10 \x0301,00 Sent: "
    TAIL = " \x0310,10 \x0304,05 \r\n"

    def build(self, value):
        return f"{self.HEAD}{value}{self.TAIL}"

    def test_budget_is_the_pessimistic_420(self):
        """Defect: the 512-byte cap was used as if the whole line were ours.

        The server prepends ":nick!ident@host " when relaying, so the usable
        room is 512 minus a worst-case prefix minus the CRLF. Widening this
        constant back towards 512 brings the mid-colour-code truncation back.
        """
        self.assertEqual(announce.IRC_LINE_BUDGET, 420)

    def test_short_value_is_returned_unchanged(self):
        """A line that already fits must not be touched - no stray ellipsis."""
        line = announce.fit_irc_line(self.build, "Enter Sandman.flac")
        self.assertEqual(line, self.build("Enter Sandman.flac"))
        self.assertNotIn("...", line)

    def test_long_value_is_trimmed_until_the_line_fits(self):
        """Defect: an over-long field was emitted whole and the server cut it."""
        line = announce.fit_irc_line(self.build, "a" * 900)
        self.assertLessEqual(encoded_len(line), announce.IRC_LINE_BUDGET)
        self.assertIn("...", line)
        # It must fill the budget rather than trimming to something tiny.
        self.assertGreater(encoded_len(line), announce.IRC_LINE_BUDGET - 10)

    def test_non_ascii_is_measured_in_bytes_not_characters(self):
        """Defect: length was counted in characters, so umlauts overflowed.

        400 'a'-umlauts are 800 bytes. A character-counting implementation
        would happily keep ~420 of them and emit an 800-byte line.
        """
        value = "ä" * 400
        line = announce.fit_irc_line(self.build, value)
        self.assertLessEqual(encoded_len(line), announce.IRC_LINE_BUDGET)
        # Two bytes per umlaut means the character count has to come in far
        # below the byte budget; this is what proves bytes were measured.
        self.assertLess(len(line), announce.IRC_LINE_BUDGET - 100)
        self.assertGreater(encoded_len(line), announce.IRC_LINE_BUDGET - 10)

    def test_trimmed_line_keeps_the_template_tail_and_colour_codes(self):
        """Defect: cutting the finished line sliced colour codes in half.

        Re-rendering from the template is what guarantees the fixed tail - and
        every mIRC control byte in it - survives the trim intact.
        """
        line = announce.fit_irc_line(self.build, "b" * 2000)
        self.assertTrue(line.startswith(self.HEAD))
        self.assertTrue(line.endswith(self.TAIL))
        # Same number of colour and formatting bytes as an untrimmed render.
        reference = self.build("x")
        self.assertEqual(line.count("\x03"), reference.count("\x03"))

    def test_impossible_template_returns_instead_of_hanging(self):
        """Defect risk: a template larger than the budget could loop forever.

        The shrink loop stops when the variable is empty, so a structurally
        over-long template returns (noisily) rather than spinning on the IRC
        read thread.
        """
        fat = "z" * 600

        def build(value):
            return f"PRIVMSG #x :{fat}{value}\r\n"

        started = time.perf_counter()
        line = announce.fit_irc_line(build, "y" * 100)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0)
        self.assertIn(fat, line)
        # Every scrap of the caller's value was given up trying to fit.
        self.assertNotIn("y", line)


class TransferCompleteLineTests(QuietTestCase):
    """The Sent: announcement carries a filename straight off the disk."""

    def setUp(self):
        super().setUp()
        # db.load_advanced_stats reads config.STATS_FILE. Point it at a path
        # inside a throwaway directory so the real data/ dir is never touched
        # and the numbers in the line are the deterministic defaults.
        stats_dir = tempfile.mkdtemp(prefix="dccore-stats-")
        self.addCleanup(_rmtree, stats_dir)
        self.config.STATS_FILE = os.path.join(stats_dir, "stats.txt")
        # send_transfer_complete also logs to the debug channel; capture that.
        self.addCleanup(setattr, announce, "send_debug", _REAL_SEND_DEBUG)
        self.debug_lines = silence_debug(announce)

    def announced(self):
        """The single channel line the fake oserve recorded."""
        self.assertEqual(len(self.oserve.queued), 1, "expected exactly one announcement")
        target, message, _is_vip = self.oserve.queued[0]
        self.assertEqual(target, "channel_announce")
        return message

    def test_long_filename_is_clamped_with_an_ellipsis(self):
        """Defect: a 158-char classical track name blew the line past 512 bytes.

        The channel then saw the announcement truncated mid-colour-code, which
        smears the background colour across the rest of the client's line.
        """
        self.assertEqual(len(CLASSICAL_TRACK), 158)
        announce.send_transfer_complete(
            "#dccore-test", "dave", CLASSICAL_TRACK, 41_000_000, time.time() - 30, 512000
        )
        line = self.announced()
        self.assertLessEqual(encoded_len(line), announce.IRC_LINE_BUDGET)
        self.assertIn("...", line)
        # The template survived: the tail block and the recipient are still there.
        self.assertIn("dave", line)
        self.assertTrue(line.endswith("\r\n"))
        # A recognisable head of the filename is kept, not just an ellipsis.
        self.assertIn("Ludwig van Beethoven", line)

    def test_short_filename_is_left_untouched(self):
        """A normal filename must reach the channel verbatim, no ellipsis."""
        announce.send_transfer_complete(
            "#dccore-test", "dave", "01 - Enter Sandman.flac", 4096, time.time() - 2, 512000
        )
        line = self.announced()
        self.assertLess(encoded_len(line), announce.IRC_LINE_BUDGET)
        self.assertIn("01 - Enter Sandman.flac", line)
        self.assertNotIn("...", line)

    def test_debug_note_carries_the_full_filename(self):
        """The channel line is clamped; the debug log still names the file."""
        announce.send_transfer_complete(
            "#dccore-test", "dave", CLASSICAL_TRACK, 41_000_000, time.time() - 30, 512000
        )
        self.assertTrue(self.debug_lines, "transfer completion produced no debug line")
        category, text = self.debug_lines[-1]
        self.assertEqual(category, "INFO")
        self.assertIn(CLASSICAL_TRACK, text)


class SearchResultHeaderTests(QuietTestCase):
    """@find headers echo a term any user in the channel can type."""

    def header(self):
        self.assertEqual(len(self.oserve.queued), 1, "expected exactly one header")
        return self.oserve.queued[0][1]

    def test_long_search_term_is_clamped(self):
        """Defect: search_term was only length-CHECKED at the low end.

        Nothing capped it, so any user could type a 500-character @find and the
        private header came back over 512 bytes and was cut mid-colour-code.
        """
        announce.send_search_result_header("dave", "metallica" * 60, 7, "#dccore-test")
        line = self.header()
        self.assertLessEqual(encoded_len(line), announce.IRC_LINE_BUDGET)
        self.assertIn("...", line)
        self.assertIn("metallica", line)
        self.assertTrue(line.endswith("\r\n"))

    def test_non_ascii_search_term_is_clamped_in_bytes(self):
        """Defect: a multi-byte term overflowed even when its char count fitted."""
        announce.send_search_result_header("dave", "ä" * 400, 3, "#dccore-test")
        line = self.header()
        self.assertLessEqual(encoded_len(line), announce.IRC_LINE_BUDGET)
        self.assertLess(len(line), announce.IRC_LINE_BUDGET)

    def test_ordinary_search_term_is_untouched(self):
        """A normal term must be echoed back exactly, with no ellipsis."""
        announce.send_search_result_header("dave", "metallica", 7, "#dccore-test")
        line = self.header()
        self.assertIn("metallica", line)
        self.assertNotIn("...", line)


class DebugPumpTests(QuietTestCase):
    """send_debug must hand off, not block, and must label alerts correctly."""

    def setUp(self):
        super().setUp()
        # Use the genuine implementation whatever an earlier module left behind.
        announce.send_debug = _REAL_SEND_DEBUG
        self.addCleanup(setattr, announce, "send_debug", _REAL_SEND_DEBUG)
        # The drain only writes when BOTH gates are open. oserve.irc_connection
        # is None on the fresh stub, so nothing is popped and the queue is a
        # stable place to inspect what send_debug produced.
        self.assertIsNone(self.oserve.irc_connection)
        announce._debug_queue.clear()
        self.addCleanup(announce._debug_queue.clear)
        self.config.DEBUG_CHANNEL = "#dccore-debug"

    def last_line(self):
        self.assertTrue(announce._debug_queue, "send_debug queued nothing")
        return announce._debug_queue[-1]

    def test_send_debug_is_non_blocking(self):
        """Defect: send_debug slept 0.5s under a lock, on the IRC read thread.

        security.check_user_status calls it once per denied PRIVMSG, so a
        netsplit's worth of banned nicks reconnecting stalled the read loop for
        fifteen seconds - no PONG answered, no request dispatched. Thirty calls
        must now cost essentially nothing.
        """
        started = time.perf_counter()
        for i in range(30):
            announce.send_debug(f"blocked nick_{i}", category="BAN")
        elapsed = time.perf_counter() - started
        # The pre-fix code needed ~15s for this loop. A generous ceiling still
        # catches any return of a per-call sleep.
        self.assertLess(elapsed, 1.0, f"send_debug blocked for {elapsed:.2f}s")
        # Non-blocking must not mean "silently dropped".
        self.assertEqual(len(announce._debug_queue), 30)

    def test_debug_queue_is_bounded(self):
        """Defect: an alert flood grew the backlog without limit.

        The deque has a maxlen, so a flood drops the OLDEST lines instead of
        growing forever (or stalling the caller until it drains).
        """
        self.assertEqual(announce._debug_queue.maxlen, 200)
        for i in range(300):
            announce.send_debug(f"flood-marker-{i:03d}")
        self.assertEqual(len(announce._debug_queue), 200)
        joined = "".join(announce._debug_queue)
        self.assertNotIn("flood-marker-000", joined)
        self.assertNotIn("flood-marker-099", joined)
        self.assertIn("flood-marker-100", joined)
        self.assertIn("flood-marker-299", joined)

    def test_hardban_renders_a_security_label(self):
        """Defect: HARDBAN alerts fell through to the grey [INFO] tag.

        dcc.py raises HARDBAN for a blocked path traversal and for a poisoned
        queue entry. Rendered as [INFO] they looked exactly like routine
        chatter, so a filesystem probing campaign scrolled past unnoticed.
        """
        announce.send_debug("Blocked traversal from mallory", category="HARDBAN")
        line = self.last_line()
        self.assertIn("[SECURITY]", line)
        self.assertNotIn("[INFO]", line)
        self.assertNotIn(self.config.C_GREY, line)
        self.assertIn(self.config.C_RED, line)
        self.assertIn("Blocked traversal from mallory", line)

    def test_hardban_is_distinguishable_from_an_admin_ban(self):
        """Defect: both alert kinds would have shared one label.

        The "BAN" category is an admin confirming !ban and renders [HARDBAN];
        the "HARDBAN" category is someone probing the filesystem and renders
        [SECURITY]. Routine administration must not look like an attack.
        """
        announce.send_debug("FLAC banned mallory", category="BAN")
        admin_line = self.last_line()
        announce.send_debug("Blocked traversal from mallory", category="HARDBAN")
        security_line = self.last_line()

        self.assertIn("[HARDBAN]", admin_line)
        self.assertNotIn("[SECURITY]", admin_line)
        self.assertIn("[SECURITY]", security_line)
        self.assertNotIn("[HARDBAN]", security_line)

    def test_unknown_category_falls_back_to_grey_info(self):
        """The default tag is still the grey [INFO] used for ordinary chatter."""
        announce.send_debug("just a note", category="SOMETHING-ELSE")
        line = self.last_line()
        self.assertIn("[INFO]", line)
        self.assertIn(self.config.C_GREY, line)

    def test_line_is_addressed_to_the_debug_channel(self):
        """The pump must still produce a well-formed PRIVMSG to DEBUG_CHANNEL."""
        self.config.DEBUG_CHANNEL = "#dccore-debug"
        announce.send_debug("hello")
        line = self.last_line()
        self.assertTrue(line.startswith("PRIVMSG #dccore-debug :"))
        self.assertTrue(line.endswith("\r\n"))


class DebugDrainDeliveryTests(QuietTestCase):
    """The background drain is what actually puts the queued lines on the wire."""

    def setUp(self):
        super().setUp()
        announce.send_debug = _REAL_SEND_DEBUG
        self.addCleanup(setattr, announce, "send_debug", _REAL_SEND_DEBUG)
        announce._debug_queue.clear()
        self.socket = RecordingSocket()
        # Close the gate again for whatever runs next, so a live drain can
        # never swallow another test's queued lines.
        self.addCleanup(self._close_gate)
        # Speed the pump up; the default pause between lines is 0.5s.
        self.config.DEBUG_MSG_DELAY = 0.01

    def _close_gate(self):
        self.oserve.irc_connection = None
        announce._debug_queue.clear()

    def wait_for(self, predicate, timeout=1.5):
        """Poll until the drain thread has had its chance."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_queued_line_reaches_the_socket(self):
        """Defect: the hand-off must still deliver, not just return fast.

        Both gates open - a socket published and the debug channel joined - so
        the drain thread writes the line the caller never waited for.
        """
        self.config.bot_joined_channel = True
        self.oserve.irc_connection = self.socket
        announce.send_debug("drain-delivery-marker", category="INFO")
        delivered = self.wait_for(lambda: "drain-delivery-marker" in self.socket.text())
        self.assertTrue(delivered, "queued debug line never reached the socket")
        self.assertIn("PRIVMSG", self.socket.text())

    def test_lines_are_held_until_the_debug_channel_is_joined(self):
        """Defect: draining on the socket alone ate the reconnect backlog.

        irc.py publishes oserve.irc_connection seconds before the debug channel
        has been joined. Flushing on that signal alone threw the outage report
        at a server that rejects it - and popping first meant the lines were
        gone either way.
        """
        self.config.bot_joined_channel = False
        self.oserve.irc_connection = self.socket
        announce.send_debug("held-until-joined-marker", category="INFO")
        # Long enough for the drain to have looped several times.
        time.sleep(0.4)
        self.assertEqual(self.socket.sent, [], "line was flushed before the join")
        self.assertIn(
            "held-until-joined-marker",
            "".join(announce._debug_queue),
            "line was popped and lost while the channel was unjoined",
        )


class TransferSpeedLockUsesDccQueueLock(unittest.TestCase):
    """announce_worker()'s per-cycle transfer-speed calculation iterates
    config.active_transfers - the exact list dcc.py appends to and filters
    in place under dcc.queue_lock (dcc.py's own module-level lock) every
    time a transfer starts or ends. This block used to guard its OWN read
    with config.queue_lock instead - a completely separate Lock() object
    oserve.py allocated - so it excluded nothing dcc.py's writers were
    doing: two different locks "protecting" the same list is the same as
    no lock at all between the two sides.

    Reads the daemon's own source rather than re-typing the condition, so an
    edit that reintroduces config.queue_lock (or a bare fallback
    threading.Lock()) fails this test instead of silently reopening the race -
    the same discipline test_irc_dispatch.py already uses for its own
    dispatch-chain conditions.

    Written for announce.py, where the scan used to live. It now lives in
    stats_mgr.live_speed(), because the dashboard wants the same figure and
    sampling it twice would have each caller measure a fraction of the
    movement. So these look for the scan WHEREVER it is rather than in one
    named file: the property is that this loop is guarded, not that it sits in
    a particular module, and pinning the location would have to be rewritten
    every time it moves.
    """

    # Only the modules that SAMPLE the rate. dcc.py has its own scan of the
    # same list at the bottom of start_dcc_send(), updating a transfer's own
    # bytes_sent as chunks go out - unrelated bookkeeping that happens to share
    # the loop's shape, and including it here would make this test fail on
    # something it is not about.
    SOURCES = ("announce.py", "stats_mgr.py")

    def setUp(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.by_file = {}
        for name in self.SOURCES:
            path = os.path.join(repo_root, name)
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                self.by_file[name] = handle.read().splitlines()
        self.lines = self.by_file.get("announce.py", [])

    def _code_lines_containing(self, fragment, lines=None):
        """Lines containing `fragment`, excluding comments - so a module's own
        explanatory comments about the historical bug do not trip the very
        tests written to guard against it."""
        return [line.strip() for line in (self.lines if lines is None else lines)
                if fragment in line and not line.strip().startswith("#")]

    def _scans(self):
        """[(file, index)] for every live scan of config.active_transfers."""
        found = []
        for name, lines in sorted(self.by_file.items()):
            for index, line in enumerate(lines):
                if ("for tx in config.active_transfers:" in line
                        and not line.strip().startswith("#")):
                    found.append((name, index))
        return found

    def test_the_active_transfers_scan_is_guarded_by_dcc_queue_lock(self):
        scans = self._scans()

        self.assertEqual(
            len(scans), 1,
            "expected exactly one active_transfers scan across the daemon, "
            "found %d: %s. More than one means the sampling was copied rather "
            "than shared, and copies steal each other's measurement window."
            % (len(scans), ", ".join("%s:%d" % (f, i + 1) for f, i in scans)))

    def test_config_queue_lock_is_never_referenced(self):
        offenders = []
        for name, lines in sorted(self.by_file.items()):
            if name == "oserve.py":
                continue          # allocates it; nothing should USE it
            offenders += ["%s: %s" % (name, line)
                          for line in self._code_lines_containing("config.queue_lock", lines)]

        self.assertEqual(
            offenders, [],
            "config.queue_lock reappeared - it is a different lock object than "
            "dcc.queue_lock, which is what actually guards "
            "config.active_transfers; see dcc.py's and oserve.py's comments on "
            "this exact mistake: " + "; ".join(offenders))

    def test_the_scan_is_preceded_by_a_with_dcc_queue_lock_line(self):
        scans = self._scans()
        self.assertTrue(scans, "could not locate the active_transfers scan to check its guard")

        for name, index in scans:
            lines = self.by_file[name]
            preceding = "\n".join(lines[max(0, index - 8):index])
            self.assertIn(
                "with dcc.queue_lock:", preceding,
                "%s's active_transfers scan is no longer guarded by "
                "dcc.queue_lock - the same lock dcc.py holds for every "
                "append/removal on that list" % name)


def _rmtree(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


class TheQueuePositionNoticeReadsTheSetting(QuietTestCase):
    """announce.send_dcc_queue_notice() tells a user where they are in their
    own queue, and how long that queue may get.

    It hardcoded "of 100" while send_dcc_error()'s "user_full" message twenty
    lines above read config.MAX_USER_QUEUE properly - so the two messages
    about the same limit disagreed the moment an operator changed it, and the
    one a user sees on every queued file was the wrong one.
    """

    def notice(self):
        return "".join(m for _u, m, *_ in self.oserve.queued)

    def test_it_quotes_the_configured_limit(self):
        self.set_config(MAX_USER_QUEUE=20)

        announce.send_dcc_queue_notice("dave", "Song.flac", 5)

        text = self.notice()
        self.assertIn("20", text)
        self.assertNotIn("of 100", text)

    def test_the_default_still_reads_correctly(self):
        """Control: a limit that happens to equal the old literal must still
        come from the setting, not from the literal being left in place."""
        self.set_config(MAX_USER_QUEUE=100)

        announce.send_dcc_queue_notice("dave", "Song.flac", 3)

        self.assertIn("100", self.notice())

    def test_it_agrees_with_the_queue_full_error(self):
        """The two messages describe one limit and must not disagree - which
        is the whole defect, stated as a test."""
        self.set_config(MAX_USER_QUEUE=42)

        announce.send_dcc_queue_notice("dave", "Song.flac", 1)
        queued_text = self.notice()
        self.oserve.queued.clear()
        announce.send_dcc_error("dave", "user_full")
        full_text = self.notice()

        self.assertIn("42", queued_text)
        self.assertIn("42", full_text)


class TheAdvertNeverPublishesTheNoListSentinel(unittest.TestCase):
    """#229: list.get_file_count_date_size_and_raw_bytes() answers "No List"
    as the DATE when no master list exists yet - a fresh install before its
    first !update. Unguarded, that string reached the advert verbatim:
    "...For My List Of: 0 Files (0B) created No List", published into every
    channel every ANNOUNCE_INTERVAL until the first list build finished.
    commands.py's -stats reply already guarded the same sentinel; the advert
    - far more publicly visible - never had the matching guard.

    announce_worker() is a `while True:` loop that owns the process (see
    tests/uncovered_functions.txt) and cannot be driven directly the way most
    of this suite drives its targets, so this reads the source structurally:
    a check for the sentinel must exist, and it must come BEFORE the
    announce_msg template is built - the same "order is the whole fix" shape
    #191's launcher test uses for an analogous reason.
    """

    def _source(self):
        import io as _io
        with _io.open(announce.__file__, encoding="utf-8") as handle:
            return handle.read()

    def test_the_sentinel_is_checked_before_the_message_is_built(self):
        source = self._source()

        guard_at = source.find('list_date == "No List"')
        message_at = source.find("announce_msg = (")

        self.assertNotEqual(guard_at, -1,
                            "no check for the \"No List\" sentinel found in announce.py")
        self.assertNotEqual(message_at, -1,
                            "fixture invariant: could not find the advert template "
                            "build - the scan is broken, not the code")
        self.assertLess(guard_at, message_at,
                        "the sentinel check comes AFTER the advert template is "
                        "already built, which is too late to skip publishing it")

    def test_the_guard_actually_skips_rather_than_falling_through(self):
        """The old "if False:"-shaped survivor this whole session has been
        finding elsewhere: a check that exists but is never reached would
        satisfy the test above while changing nothing. Parsed with ast so a
        `continue`/`return`/`break` in the same if-body counts, and a check
        that merely logs and falls through does not."""
        import ast

        tree = ast.parse(self._source())
        found_skip = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            is_sentinel_check = (
                isinstance(test, ast.Compare)
                and any(isinstance(comp, ast.Constant) and comp.value == "No List"
                       for comp in test.comparators))
            if not is_sentinel_check:
                continue
            if any(isinstance(stmt, (ast.Continue, ast.Return, ast.Break))
                  for stmt in node.body):
                found_skip = True

        self.assertTrue(found_skip,
                        "found a \"No List\" check, but its body does not "
                        "continue/return/break - it would fall through and "
                        "publish the sentinel anyway")


if __name__ == "__main__":
    unittest.main()
