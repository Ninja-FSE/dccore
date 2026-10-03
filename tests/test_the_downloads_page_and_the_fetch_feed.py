"""#1019: what the bot leeches from other bots reaches the operator.

Three things an operator asked for:

- the Downloads page sorts, pages (10/15/20/50/100 a page) and clears its
  finished rows in one click;
- @DCCore says what the bot itself fetches (a FETCH feed line per step);
- @DCCore's side panel has a Downloading section beside Sending (a FETCHING
  line per file in flight, in the status burst).
"""

import io
import json
import os
import re
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import theme  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def row(state, bot="SomeBot", name="a.flac", at=1.0, **more):
    base = {"bot": bot, "filename": name, "requested_filename": name, "state": state,
            "request_type": "file", "requested_at": at}
    base.update(more)
    return base


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


class ClearingTheFinishedRows(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        config.fetch_queue.update({
            "1": row("complete"), "2": row("complete"), "3": row("failed"),
            "4": row("pending"), "5": row("receiving"), "6": row("queued"),
            "7": row("offered"), "8": row("listening"),
        })

    def test_finished_forgets_downloaded_and_failed_only(self):
        status, result = webserver.build_fetch_clear_result({"which": "finished"})
        self.assertEqual((status, result), (200, {"cleared": 3}))
        self.assertEqual(sorted(config.fetch_queue), ["4", "5", "6", "7", "8"])

    def test_no_which_means_finished(self):
        self.assertEqual(webserver.build_fetch_clear_result({})[1], {"cleared": 3})
        self.assertEqual(webserver.build_fetch_clear_result(None)[1], {"cleared": 0})

    def test_complete_leaves_the_failures_for_a_look(self):
        self.assertEqual(webserver.build_fetch_clear_result({"which": "complete"})[1], {"cleared": 2})
        self.assertEqual(config.fetch_queue["3"]["state"], "failed")

    def test_failed_leaves_the_downloads(self):
        self.assertEqual(webserver.build_fetch_clear_result({"which": "failed"})[1], {"cleared": 1})
        self.assertIn("1", config.fetch_queue)

    def test_anything_else_is_refused_and_nothing_goes(self):
        for which in ("pending", "all", "receiving"):
            status, result = webserver.build_fetch_clear_result({"which": which})
            self.assertEqual(status, 400, which)
            self.assertIn("error", result)
        self.assertEqual(len(config.fetch_queue), 8)

    def test_a_second_clear_finds_nothing(self):
        webserver.build_fetch_clear_result({"which": "finished"})
        self.assertEqual(webserver.build_fetch_clear_result({"which": "finished"})[1], {"cleared": 0})

    def test_the_history_file_follows(self):
        calls = []
        real = dcc_fetch.persist_fetch_history
        dcc_fetch.persist_fetch_history = lambda: calls.append(1)
        self.addCleanup(setattr, dcc_fetch, "persist_fetch_history", real)
        webserver.build_fetch_clear_result({"which": "finished"})
        webserver.build_fetch_clear_result({"which": "finished"})
        self.assertEqual(len(calls), 1, "persisted once for the clear that removed rows, not for the empty one")


class TheFetchFeed(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        dcc_fetch._fetch_feed_told.clear()
        dcc_fetch._fetch_feed_seeded[0] = False
        self.told = []
        self.real = announce.feed_event
        announce.feed_event = lambda kind, text, **fields: self.told.append((kind, text, fields))
        self.addCleanup(setattr, announce, "feed_event", self.real)

    def test_the_first_pass_only_remembers(self):
        config.fetch_queue["1"] = row("complete")
        dcc_fetch.tell_the_fetch_feed()
        self.assertEqual(self.told, [])

    def test_a_row_that_moves_is_told_once_per_step(self):
        dcc_fetch.tell_the_fetch_feed()
        config.fetch_queue["1"] = row("offered")
        dcc_fetch.tell_the_fetch_feed()
        dcc_fetch.tell_the_fetch_feed()
        config.fetch_queue["1"]["state"] = "receiving"
        config.fetch_queue["1"]["total_size"] = 4 * 1024 * 1024
        dcc_fetch.tell_the_fetch_feed()
        config.fetch_queue["1"].update(state="complete", bytes_received=4 * 1024 * 1024)
        dcc_fetch.tell_the_fetch_feed()
        self.assertEqual([f["action"] for _k, _t, f in self.told], ["asked", "receiving", "done"])
        self.assertTrue(all(k == "FETCH" and f["bot"] == "SomeBot" for k, _t, f in self.told))
        self.assertIn('"a.flac"', self.told[0][1])
        self.assertIn("SomeBot", self.told[2][1])

    def test_a_failure_carries_its_reason(self):
        dcc_fetch.tell_the_fetch_feed()
        config.fetch_queue["1"] = row("failed", reason="Bot refused")
        dcc_fetch.tell_the_fetch_feed()
        (_k, text, fields), = self.told
        self.assertEqual(fields["action"], "failed")
        self.assertIn("Bot refused", text)

    def test_a_queue_position_is_named(self):
        dcc_fetch.tell_the_fetch_feed()
        config.fetch_queue["1"] = row("queued", queue_position=3)
        dcc_fetch.tell_the_fetch_feed()
        self.assertIn("#3", self.told[0][1])

    def test_a_file_list_is_left_to_listfetch(self):
        dcc_fetch.tell_the_fetch_feed()
        config.fetch_queue["1"] = row("complete", request_type="list")
        dcc_fetch.tell_the_fetch_feed()
        self.assertEqual(self.told, [])

    def test_waiting_states_are_not_told(self):
        dcc_fetch.tell_the_fetch_feed()
        config.fetch_queue["1"] = row("pending")
        config.fetch_queue["2"] = row("listening")
        dcc_fetch.tell_the_fetch_feed()
        self.assertEqual(self.told, [])

    def test_a_forgotten_row_is_forgotten_by_the_feed_too(self):
        dcc_fetch.tell_the_fetch_feed()
        config.fetch_queue["1"] = row("offered")
        dcc_fetch.tell_the_fetch_feed()
        del config.fetch_queue["1"]
        dcc_fetch.tell_the_fetch_feed()
        self.assertEqual(dcc_fetch._fetch_feed_told, {})

    def test_a_console_that_raises_does_not_stop_the_dispatcher(self):
        def boom(*_a, **_k):
            raise RuntimeError("console gone")
        announce.feed_event = boom
        dcc_fetch.tell_the_fetch_feed()
        config.fetch_queue["1"] = row("offered")
        dcc_fetch.tell_the_fetch_feed()

    def test_the_dispatcher_calls_it(self):
        src = read("src", "dcc_fetch.py")
        body = src[src.index("def fetch_dispatcher_worker"):]
        self.assertIn("tell_the_fetch_feed()", body.split("\ndef ")[0])

    def test_a_restart_clears_the_receive_clock(self):
        form = dcc_fetch._restart_form(row("receiving", receiving_since=5.0, bytes_received=9))
        self.assertNotIn("receiving_since", form)


class TheFetchLineOnTheWire(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        self.session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        self.session.authenticated = True
        self.session.structured = True
        announce.add_event_sink(self.session.event_sink)
        self.addCleanup(announce.remove_event_sink, self.session.event_sink)

    def test_the_line_names_the_bot_the_action_and_the_text(self):
        announce.feed_event("FETCH", 'Fetched "a.flac" from SomeBot (4.0MB)', bot="SomeBot", action="done")
        self.assertIn('DCCORE FETCH SomeBot done Fetched "a.flac" from SomeBot (4.0MB)',
                      list(self.session._outbox))

    def test_it_is_a_known_kind_and_stays_off_the_channel(self):
        self.assertIn("FETCH", adminchat.FEED_KINDS)
        self.assertIn("FETCH", announce.FEED_ONLY_CATEGORIES)
        self.assertEqual(announce.category_tag("FETCH", theme.blocks())[0], "FETCH")


class TheFetchingLines(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()

    def test_a_receiving_file_gets_its_progress_and_speed(self):
        config.fetch_queue["1"] = row("receiving", bytes_received=1000, total_size=4000, receiving_since=100.0)
        self.assertEqual(adminchat.fetching_lines(now=110.0), ["DCCORE FETCHING SomeBot 1000 4000 100 a.flac"])

    def test_no_speed_until_it_has_run_a_moment(self):
        config.fetch_queue["1"] = row("receiving", bytes_received=1000, total_size=4000, receiving_since=100.0)
        self.assertTrue(adminchat.fetching_lines(now=100.2)[0].split(" ")[5] == "0")

    def test_a_row_without_a_start_or_size_still_draws(self):
        config.fetch_queue["1"] = row("receiving")
        self.assertEqual(adminchat.fetching_lines(now=5.0), ["DCCORE FETCHING SomeBot 0 0 0 a.flac"])

    def test_only_receiving_rows_are_listed_oldest_first(self):
        config.fetch_queue["1"] = row("receiving", name="late.flac", at=9.0)
        config.fetch_queue["2"] = row("complete")
        config.fetch_queue["3"] = row("queued")
        config.fetch_queue["4"] = row("receiving", name="early.flac", at=2.0)
        lines = adminchat.fetching_lines(now=1.0)
        self.assertEqual([l.split(" ")[-1] for l in lines], ["early.flac", "late.flac"])

    def test_a_list_is_named_for_its_bot(self):
        config.fetch_queue["1"] = row("receiving", request_type="list", name="")
        self.assertTrue(adminchat.fetching_lines(now=1.0)[0].endswith("SomeBot's file list"))

    def test_a_name_cannot_break_the_line(self):
        config.fetch_queue["1"] = row("receiving", name="a\r\nDCCORE TAKEN x.flac")
        (line,) = adminchat.fetching_lines(now=1.0)
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)

    def test_the_status_burst_carries_them_only_when_asked(self):
        config.fetch_queue["1"] = row("receiving", bytes_received=5, total_size=10)
        self.assertFalse([l for l in adminchat.status_lines() if l.startswith("DCCORE FETCHING")])
        self.assertTrue([l for l in adminchat.status_lines(fetching=True) if l.startswith("DCCORE FETCHING")])

    def test_only_a_script_that_can_draw_them_is_sent_them(self):
        self.assertFalse(adminchat.script_draws_fetching("1.7"))
        self.assertFalse(adminchat.script_draws_fetching(""))
        self.assertFalse(adminchat.script_draws_fetching("garbage"))
        self.assertTrue(adminchat.script_draws_fetching("1.8"))
        self.assertTrue(adminchat.script_draws_fetching("1.8.1"))
        self.assertTrue(adminchat.script_draws_fetching("1.10"))

    def test_hello_records_it(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        session.authenticated = True
        adminchat._cmd_hello(session, "dccore.mrc 1.8")
        self.assertTrue(session.draws_fetching)
        adminchat._cmd_hello(session, "dccore.mrc 1.7")
        self.assertFalse(session.draws_fetching)


class ASecondOfferForAFinishedFile(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()

    def offer(self, name):
        import io as _io
        from contextlib import redirect_stdout
        out = _io.StringIO()
        with redirect_stdout(out):
            dcc_fetch.handle_incoming_offer(None, "SomeBot", f'DCC SEND "{name}" 1 2 3')
        return out.getvalue()

    def test_it_is_ignored_and_called_a_duplicate(self):
        config.fetch_queue["1"] = row("complete", name="a b.flac")
        said = self.offer("a_b.flac")
        self.assertIn("already fetched (request 1)", said)
        self.assertNotIn("unsolicited", said)
        self.assertEqual(config.fetch_queue["1"]["state"], "complete")

    def test_a_stranger_is_still_unsolicited(self):
        config.fetch_queue["1"] = row("complete", name="a.flac")
        self.assertIn("unsolicited", self.offer("other.flac"))

    def test_another_bots_file_of_that_name_is_not_a_duplicate(self):
        config.fetch_queue["1"] = row("complete", bot="ElseBot", name="a.flac")
        self.assertIn("unsolicited", self.offer("a.flac"))


class ALateAnswerIsStillTheAnswer(DCCoreTestCase):
    """A bot with a busy queue sent the file two minutes after our offer
    timeout had failed the row, and it was refused as unsolicited."""

    def claim(self, **more):
        import time
        config.fetch_queue.clear()
        config.fetch_queue["1"] = row("failed", name="It's A, Song (x).mp3", reason="no response",
                                      offered_at=time.time() - 5, **more)
        return dcc_fetch._claim_matching_offer_locked(
            config.fetch_queue, "somebot", "It's_A,_Song_(x).mp3")

    def test_a_row_that_failed_for_silence_takes_it(self):
        rid, found = self.claim()
        self.assertEqual(rid, "1")
        self.assertEqual(found["state"], "receiving")
        self.assertNotIn("reason", found)

    def test_a_long_gone_request_does_not(self):
        import time
        config.fetch_queue.clear()
        config.fetch_queue["1"] = row("failed", reason="no response", offered_at=time.time() - 99999)
        self.assertEqual(dcc_fetch._claim_matching_offer_locked(
            config.fetch_queue, "somebot", "a.flac"), (None, None))

    def test_a_row_that_failed_for_another_reason_does_not(self):
        import time
        config.fetch_queue.clear()
        config.fetch_queue["1"] = row("failed", reason="refused: no slots", offered_at=time.time())
        self.assertEqual(dcc_fetch._claim_matching_offer_locked(
            config.fetch_queue, "somebot", "a.flac"), (None, None))

    def test_another_bots_offer_does_not(self):
        import time
        config.fetch_queue.clear()
        config.fetch_queue["1"] = row("failed", reason="no response", offered_at=time.time())
        self.assertEqual(dcc_fetch._claim_matching_offer_locked(
            config.fetch_queue, "someoneelse", "a.flac"), (None, None))


class TheReceiverAcknowledges(DCCoreTestCase):
    """A DCCore sender counts a file as sent only once the whole of it is
    acknowledged; a receiver that never does looks like a failed send and is
    offered the file again (#1019)."""

    def transfer(self, pieces, close_after=False):
        import shutil
        import struct
        import tempfile
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        for piece in pieces:
            theirs.sendall(piece)
        total = sum(len(p) for p in pieces)
        if close_after:
            theirs.close()
        config.fetch_queue.clear()
        r = row("receiving")
        config.fetch_queue["1"] = r
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)

        class Tcp:
            def getpeername(self):
                return ("127.0.0.1", 50000)

            def __getattr__(self, name):
                return getattr(ours, name)

        dcc_fetch._run_transfer(r, {"size": total, "ip": None, "port": 0}, dest, "t.flac", sock=Tcp())
        acks = b""
        if not close_after:
            theirs.settimeout(0.5)
            try:
                while len(acks) < 4 * len(pieces):
                    data = theirs.recv(4096)
                    if not data:
                        break
                    acks += data
            except socket.timeout:
                pass
        words = [struct.unpack("!I", acks[i:i + 4])[0] for i in range(0, len(acks) - 3, 4)]
        return r, total, words

    def test_the_whole_size_is_acknowledged_at_the_end(self):
        r, total, words = self.transfer([b"x" * 100])
        self.assertEqual(r["state"], "complete")
        self.assertEqual(words[-1], total)

    def test_the_count_is_cumulative_and_never_goes_back(self):
        r, total, words = self.transfer([b"x" * 4096])
        self.assertEqual(r["state"], "complete")
        self.assertEqual(sorted(words), words)
        self.assertEqual(words[-1], 4096)

    def test_a_sender_that_hung_up_after_the_last_byte_still_completes(self):
        r, _total, _words = self.transfer([b"x" * 100], close_after=True)
        self.assertEqual(r["state"], "complete")

    def test_a_sender_that_never_reads_acks_is_only_tried_once(self):
        import shutil
        import tempfile
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        pieces = [b"x" * 10, b"y" * 10, b"z" * 10]
        for piece in pieces:
            theirs.sendall(piece)
        config.fetch_queue.clear()
        r = row("receiving")
        config.fetch_queue["1"] = r
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        sends = []

        class Tcp:
            def getpeername(self):
                return ("127.0.0.1", 50000)

            def sendall(self, data):
                sends.append(data)
                raise OSError("nobody reads")

            def __getattr__(self, name):
                return getattr(ours, name)

        dcc_fetch._run_transfer(r, {"size": 30, "ip": None, "port": 0}, dest, "t.flac", sock=Tcp())
        self.assertEqual(r["state"], "complete")
        self.assertEqual(len(sends), 1)


class TheDownloadQueueIsShownAndCancellable(unittest.TestCase):
    """Cancel did nothing on some rows, and nothing on the page said what the
    queue held (#1021)."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as fh:
            self.js = fh.read()
        with open(os.path.join(REPO_ROOT, "web", "index.html"), encoding="utf-8") as fh:
            self.html = fh.read()

    def test_a_request_the_bot_has_not_answered_has_a_cancel_button(self):
        self.assertRegex(self.js, r'state === "queued" \|\| state === "offered"\) \{\s*//[^\n]*\n\s*action = ')
        self.assertIn('state === "queued" || state === "offered");', self.js)

    def test_the_table_is_not_rebuilt_when_nothing_changed(self):
        self.assertIn("function setDownloadsBody(box, html)", self.js)
        self.assertNotIn(".body.innerHTML =", self.js.replace(
            "box.body.innerHTML = html;", ""))

    def test_the_queue_finished_and_failed_are_three_boxes_sorted_on_their_own(self):
        for box in ("queue", "finished", "failed"):
            for part in ("table", "body", "prev", "next", "pageinfo", "count"):
                self.assertIn(f'id="downloads-{box}-{part}"', self.html, (box, part))
            
        head = self.html[self.html.index('id="downloads-queue-table"'):self.html.index('id="downloads-finished-table"')]
        self.assertEqual(head.count("data-sort-col="), 4)
        for lang in ("en", "fr", "es"):
            with open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"), encoding="utf-8") as fh:
                words = json.load(fh)
            for key in ("queue", "finished", "failed", "finishedEmpty", "failedEmpty"):
                self.assertTrue(words.get("download.box." + key), (lang, key))
        self.assertIn('DOWNLOADS_SORT_KEY + "-" + id', self.js)
        self.assertIn("sortedDownloads(mine, box.id)", self.js)

    def test_only_the_queue_box_takes_the_rows_still_waiting_or_running(self):
        block = self.js[self.js.index("var DOWNLOAD_QUEUE_STATES"):self.js.index("// Running ones first")]
        for st in ("pending", "offered", "queued", "listening", "receiving"):
            self.assertIn(f'"{st}"', block.split("var DOWNLOAD_BOXES")[0])
        self.assertIn('row.state === "complete"', block)
        self.assertIn('row.state === "failed"', block)

    def test_a_summary_box_counts_the_queue_by_where_each_request_stands(self):
        self.assertIn('id="downloads-summary"', self.html)
        for key in ("inQueue", "downloading", "asked", "queuedThere", "waiting"):
            for lang in ("en", "fr", "es"):
                with open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"), encoding="utf-8") as fh:
                    self.assertIn("download.summary." + key, json.load(fh))


class CancellingTellsTheBot(DCCoreTestCase):
    """Cancel forgot the row here while the other bot kept the file queued and
    sent it later (#1021). It now says `@<bot>-remove <file>` in the channel:
    that file only, never the bare form that clears everything we have there."""

    def setUp(self):
        super().setUp()
        import types
        from unittest import mock
        self.sent = []
        fake = types.SimpleNamespace(queue_message=lambda to, msg, *a, **k: self.sent.append((to, msg)))
        patcher = mock.patch.dict(sys.modules, {"oserve": fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        import dcc
        chan = mock.patch.object(dcc, "channel_containing_user", lambda nick: "#chan")
        chan.start()
        self.addCleanup(chan.stop)
        import runtime
        runtime.chat_peers["goodbot"] = {"#chan": 0}
        config.fetch_queue.clear()

    def put(self, rid, state, bot="GoodBot", name=None):
        config.fetch_queue[rid] = dict(dcc_fetch.new_fetch_row(bot, name or rid + ".flac"), state=state)

    def test_a_queued_request_says_which_file_to_remove(self):
        self.put("a", "queued", name="$Artist - Track 09.flac")
        status, result = webserver.build_fetch_delete_result("a")
        self.assertEqual(status, 200)
        self.assertTrue(result["removed_at_bot"])
        self.assertEqual(self.sent, [
            ("GoodBot", "PRIVMSG #chan :@GoodBot-remove $Artist - Track 09.flac\r\n")])

    def test_a_bot_not_known_to_be_dccore_is_told_nothing(self):
        """Another server may read `@<bot>-remove <file>` as the bare form and
        clear everything we have queued there."""
        self.put("a", "queued", bot="OtherServer")
        status, result = webserver.build_fetch_delete_result("a")
        self.assertEqual(status, 200)
        self.assertFalse(result["removed_at_bot"])
        self.assertEqual(self.sent, [])
        self.assertNotIn("a", config.fetch_queue, "the row is still forgotten here")

    def test_a_request_not_yet_answered_says_it_too(self):
        self.put("a", "offered")
        webserver.build_fetch_delete_result("a")
        self.assertEqual(len(self.sent), 1)

    def test_only_the_cancelled_file_is_named_when_more_wait_at_the_bot(self):
        self.put("a", "queued")
        self.put("b", "queued")
        webserver.build_fetch_delete_result("a")
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][1].endswith("-remove a.flac\r\n"))
        self.assertNotIn("b.flac", self.sent[0][1])

    def test_the_bare_form_that_clears_everything_is_never_sent(self):
        self.put("a", "queued")
        webserver.build_fetch_delete_result("a")
        self.assertNotRegex(self.sent[0][1], r"-remove\r\n")

    def test_a_request_given_up_on_for_silence_is_still_taken_back(self):
        self.put("a", "failed")
        config.fetch_queue["a"]["reason"] = "no response"
        webserver.build_fetch_delete_result("a")
        self.assertEqual(len(self.sent), 1)

    def test_a_request_that_failed_for_another_reason_says_nothing(self):
        self.put("a", "failed")
        config.fetch_queue["a"]["reason"] = "refused: no slots"
        webserver.build_fetch_delete_result("a")
        self.assertEqual(self.sent, [])

    def test_a_request_never_sent_says_nothing_to_the_bot(self):
        self.put("a", "pending")
        _status, result = webserver.build_fetch_delete_result("a")
        self.assertEqual(self.sent, [])
        self.assertNotIn("removed_at_bot", result)

    def test_a_finished_row_says_nothing_to_the_bot(self):
        self.put("a", "complete")
        webserver.build_fetch_delete_result("a")
        self.assertEqual(self.sent, [])


class TheScript(unittest.TestCase):

    def setUp(self):
        self.text = read("scripts", "mirc", "dccore.mrc")

    def test_the_version_is_the_one_the_bot_gates_on(self):
        match = re.search(r"alias dccore\.ver \{ return ([0-9.]+) \}", self.text)
        self.assertGreaterEqual(tuple(int(p) for p in match.group(1).split(".")),
                                tuple(int(p) for p in adminchat.FETCHING_SCRIPT_VERSION.split(".")))

    def test_a_fetching_line_is_stored_and_redraws_the_panel(self):
        line = next(l for l in self.text.split("\n") if "%type == FETCHING" in l)
        self.assertIn("hadd dccore.live fetch.", line)
        self.assertIn("hinc dccore.live nfetch", line)
        self.assertIn("dccore.panel.soon", line)

    def test_each_status_burst_starts_the_downloads_afresh(self):
        status = self.text[self.text.index("alias dccore.status {"):]
        status = status[:status.index("\n}")]
        self.assertIn("hdel -w dccore.live fetch.*", status)
        self.assertIn("hadd dccore.live nfetch 1", status)

    def test_the_panel_has_a_downloading_header_after_sending(self):
        panel = self.text[self.text.index("alias dccore.panel {"):]
        panel = panel[:panel.index("\n}")]
        self.assertIn("Downloading", panel)
        self.assertLess(panel.index("Sending"), panel.index("Downloading"))
        self.assertLess(panel.index("Downloading"), panel.index("Queue"))

    def test_downloading_rows_are_not_sending_rows(self):
        """dccore.sels reads a row that starts with ">" as a send to act on."""
        panel = self.text[self.text.index("alias dccore.panel {"):]
        section = panel[panel.index("Downloading"):panel.index("Queue")]
        self.assertIn("$dccore.win < $dccore.fit", section)
        self.assertNotIn("$dccore.win > ", section)

    def test_the_section_is_drawn_only_while_something_downloads(self):
        panel = self.text[self.text.index("alias dccore.panel {"):]
        self.assertIn("if ($dccore.st(fetch.1) != $null)", panel)

    def test_a_fetch_line_goes_to_the_downloads_window_not_the_main_one(self):
        """Was shown under the sends and failures tickboxes in @DCCore until #1022
        gave the feed a window of its own."""
        start = self.text.index("if (%type == FETCH) {")
        body = self.text[start:self.text.index("if (%type == TAKEN)")]
        self.assertIn("dccore.dl.log", body)
        self.assertNotIn("show.sends", body)


class TheDashboard(unittest.TestCase):

    NEW_KEYS = ("download.perPage", "download.pageOf", "download.prevPage", "download.nextPage",
                "download.clearComplete", "download.clearFailed", "download.clearTitle",
                "download.confirmClear", "download.couldNotClear")

    def test_every_language_has_the_new_words(self):
        for lang in ("en", "fr", "es"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"), encoding="utf-8") as handle:
                words = json.load(handle)
            for key in self.NEW_KEYS:
                self.assertTrue(words.get(key), f"{lang}: {key}")

    def test_the_page_has_the_controls_the_script_reads(self):
        html, js = read("web", "index.html"), read("web", "app.js")
        for ident in ("downloads-pagesize", "downloads-boxes",
                      "downloads-clear-complete", "downloads-clear-failed"):
            self.assertIn(f'id="{ident}"', html, ident)
            self.assertIn(ident, js, ident)
        for size in (10, 15, 20, 50, 100):
            self.assertIn(f'value="{size}"', html)

    def test_the_clear_call_matches_the_route(self):
        self.assertIn("/api/fetch/clear", read("web", "app.js"))
        self.assertIn('"/api/fetch/clear"', read("src", "webserver.py"))


if __name__ == "__main__":
    unittest.main()
