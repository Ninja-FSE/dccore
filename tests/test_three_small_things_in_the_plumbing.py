"""Three small things, one of which was the code lying about itself.

  * #456 `queue_mgr`'s two send sites called `send()` and discarded the count
    it returns. On Linux, with the kernel send buffer nearly full, that
    truncates an IRC line mid-message and the server reads what arrived as a
    complete command. `announce.py` has used `sendall()` all along.
  * #457 every `-que` read the whole published list off disk, for four values
    that only the EMPTY-queue layout uses - so the person who has files queued
    paid a full index read for numbers that were thrown away, on a command
    they are likely to repeat while waiting.
  * #458 `SEND_TIMEOUT = 30.0` was declared, described in a comment as the
    thing that stops a stalled peer wedging the writer, and never referenced.
    The real deadline for both directions is `sock.settimeout(1.0)`.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase, install_fake_oserve  # noqa: E402


def source(name):
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def code_only(text):
    text = re.sub(chr(35) + "[^" + chr(10) + "]*", "", text)
    return re.sub(r'"""..*?"""', "", text, flags=re.S)


class EveryOutboundLineGoesOutWhole(unittest.TestCase):
    """#456."""

    def test_neither_lane_uses_bare_send(self):
        code = code_only(source("queue_mgr.py"))

        self.assertNotIn("current_sock.send(", code,
                         "send() returns how many bytes it took and the caller "
                         "discards it, so a full kernel buffer truncates the "
                         "line")
        self.assertEqual(code.count("current_sock.sendall("), 2,
                         "both the VIP lane and the standard lane must send "
                         "the whole line")

    def test_it_encodes_the_way_the_other_writer_does(self):
        """A filename the socket cannot spell must cost a character, not raise
        on the worker thread - which is what announce.py's drain already
        does."""
        code = code_only(source("queue_mgr.py"))

        self.assertIn('encode("utf-8", errors="ignore")', code)


class TheQueueCheckReadsTheListOnlyWhenItShowsIt(DCCoreTestCase):
    """#457."""

    def setUp(self):
        super().setUp()
        install_fake_oserve()
        self.calls = []
        self._real = list_mod.get_file_count_date_size_and_raw_bytes
        self.addCleanup(setattr, list_mod,
                        "get_file_count_date_size_and_raw_bytes", self._real)
        list_mod.get_file_count_date_size_and_raw_bytes = (
            lambda *a, **k: (self.calls.append(1) or (5, "2026-01-01", "1GB", 1)))

    def run_que(self):
        commands.handle_queue_check(None, "SomeUser", "#chan")

    def test_a_user_with_files_queued_does_not_pay_for_a_list_read(self):
        config.dcc_queue["someuser"] = [{"file": "a"}]

        self.run_que()

        self.assertEqual(self.calls, [],
                         "the whole published list is read again for values "
                         "this branch never uses")

    def test_the_empty_queue_notice_still_gets_its_numbers(self):
        """The other half: the saving must not cost the feature."""
        config.dcc_queue.clear()

        self.run_que()

        self.assertEqual(len(self.calls), 1)

    def test_neither_path_raises_on_an_unset_name(self):
        """The values are referenced by the empty layout, so skipping the read
        has to leave them defined rather than unbound."""
        for queued in ([{"file": "a"}], []):
            config.dcc_queue.clear()
            if queued:
                config.dcc_queue["someuser"] = queued
            self.run_que()


class TheConsoleTimeoutIsDescribedHonestly(unittest.TestCase):
    """#458. Not a behaviour change - a constant that claimed one."""

    def test_the_dead_constant_is_gone_from_the_code(self):
        """Comments may still discuss it; nothing may assign or read it."""
        self.assertEqual(code_only(source("adminchat.py")).count("SEND_TIMEOUT"), 0)

    def test_the_comment_no_longer_promises_a_second_timeout(self):
        code = source("adminchat.py")
        block = code.split("sock.settimeout(1.0)", 1)[0][-1400:]

        self.assertNotIn("the send timeout stops a stalled peer", block,
                         "the comment still describes a send timeout that does "
                         "not exist")

    def test_it_says_why_one_second_is_the_right_answer_here(self):
        """Deleting the constant without saying why invites somebody to add it
        back - the next reader sees a one-second write deadline and reasonably
        thinks it is a mistake."""
        code = source("adminchat.py")
        block = code.split("sock.settimeout(1.0)", 1)[0][-1400:]

        self.assertIn("per SOCKET", block)
        self.assertIn("short lines", block)
