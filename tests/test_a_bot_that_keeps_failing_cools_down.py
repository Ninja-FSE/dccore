"""#1210: a bot that keeps failing is paused for a while, and the pause
survives a restart.

Every request of ours to a bot had its own asks and timeouts, but nothing
noticed that THIS bot had failed several in a row: each new request waited
out the same silence, and the channel saw the same requests again. Now
FETCH_BOT_MAX_FAILS (3) failed requests in a row - no answer, an offer we could
not connect to, a transfer that broke off - pause the bot for
FETCH_BOT_COOLDOWN_MINUTES (15), through the same pause a pause by hand uses:

- its requests wait, pending - not failed, not asked again - and the
  Downloads page and the console say "paused until 14:32 after 3 failures",
  with a Resume now button;
- the end is a stored time, saved with the other pauses, so it survives a
  restart, and it ends lazily: the first dispatcher tick after it sends the
  waiting requests, oldest first;
- a finished transfer resets the count; a refusal with a reason ("not
  found"), a "busy", an operator's cancel and an offer we turned down
  ourselves do not count.

Time passing is written into the stored end rather than slept through.
"""

import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402


class TcpLike:
    """One end of socket.socketpair(), answering getpeername() the way a TCP
    socket does - see the same class in test_a_failing_bot_is_paused.py."""

    def __init__(self, sock):
        self._sock = sock

    def getpeername(self):
        return ("127.0.0.1", 50000)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class RefusingSocket:
    """A socket whose connect() is refused at once, as a closed port is."""

    def __init__(self, *args):
        pass

    def settimeout(self, value):
        pass

    def connect(self, address):
        raise ConnectionRefusedError("connection refused")

    def close(self):
        pass


class CooldownCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#somechannel", FETCH_MAX_PER_BOT=0,
                        FETCH_BOT_MAX_FAILS=3, FETCH_BOT_COOLDOWN_MINUTES=15)
        config.channel_users["#somechannel"] = {"somebot", "otherbot"}
        self.feed = silence_debug(announce)

    def ask(self, bot="SomeBot", name="Track"):
        rid = dcc_fetch.enqueue_fetch(bot, f"{name}.flac")
        self.assertIsNotNone(rid)
        return rid

    def row(self, rid):
        return config.fetch_queue[rid]

    def asked(self, *rids):
        dcc_fetch.check_fetch_queue()
        for rid in rids:
            self.assertEqual(self.row(rid)["state"], "offered")

    def go_silent(self, *rids):
        """The bot never answered, on the last ask: the offer's time is long
        gone. One dispatcher tick fails each as "no response"."""
        for rid in rids:
            self.row(rid).update(offered_at=time.time() - 10000,
                                 silent_asks=dcc_fetch.OFFER_ASKS)
        dcc_fetch.check_fetch_queue()
        for rid in rids:
            self.assertEqual(self.row(rid)["state"], "failed")
            self.assertEqual(self.row(rid)["reason"], "no response")

    def fail_by_silence(self, count, bot="SomeBot"):
        rids = [self.ask(bot, f"Silent {n}") for n in range(count)]
        self.asked(*rids)
        self.go_silent(*rids)
        return rids

    def finish_a_transfer(self, bot="SomeBot"):
        """A real _run_transfer() over a local socket pair, to the end."""
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(b"x" * 64)
        rid = self.ask(bot, "Arrives")
        row = self.row(rid)
        row.update(state="receiving")
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        dcc_fetch._run_transfer(row, {"size": 64, "ip": None, "port": 0}, dest, "Arrives.flac",
                                sock=TcpLike(ours))
        self.assertEqual(row["state"], "complete")

    def stored(self):
        try:
            with io.open(dcc_fetch._paused_path(), encoding="utf-8") as handle:
                return json.load(handle)
        except OSError:
            return {}

    def store(self, data):
        with io.open(dcc_fetch._paused_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def cooldown(self, bot="somebot"):
        return dcc_fetch.paused_bots().get(bot)


class ThreeInARowPauseIt(CooldownCase):
    def test_three_failed_requests_pause_the_bot_for_the_cooldown(self):
        before = time.time()
        self.fail_by_silence(3)
        entry = self.cooldown()
        self.assertIsNotNone(entry, "three requests in a row failed")
        self.assertEqual(entry["by"], "cooldown")
        self.assertEqual(entry["failures"], 3)
        self.assertGreaterEqual(entry["until"], before + 15 * 60)
        self.assertLessEqual(entry["until"], time.time() + 15 * 60)
        self.assertTrue(any("Fetching from SomeBot is paused until" in text for _c, text in self.feed))

    def test_two_are_not_enough(self):
        self.fail_by_silence(2)
        self.assertIsNone(self.cooldown())

    def test_two_then_a_success_then_one_more_do_not(self):
        self.fail_by_silence(2)
        self.finish_a_transfer()
        self.fail_by_silence(1)
        self.assertIsNone(self.cooldown(), "the finished transfer started the count again")

    def test_another_bot_s_failures_are_its_own(self):
        self.fail_by_silence(2, "SomeBot")
        self.fail_by_silence(1, "OtherBot")
        self.assertEqual(dcc_fetch.paused_bots(), {})

    def test_the_setting_says_how_many(self):
        self.set_config(FETCH_BOT_MAX_FAILS=2, FETCH_BOT_COOLDOWN_MINUTES=5)
        before = time.time()
        self.fail_by_silence(2)
        entry = self.cooldown()
        self.assertEqual(entry["failures"], 2)
        self.assertGreaterEqual(entry["until"], before + 5 * 60)
        self.assertLessEqual(entry["until"], time.time() + 5 * 60)

    def test_zero_in_either_setting_turns_it_off(self):
        self.set_config(FETCH_BOT_MAX_FAILS=0)
        self.fail_by_silence(4)
        self.assertIsNone(self.cooldown())
        self.set_config(FETCH_BOT_MAX_FAILS=3, FETCH_BOT_COOLDOWN_MINUTES=0)
        self.fail_by_silence(4)
        self.assertIsNone(self.cooldown())

    def test_a_request_still_out_does_not_lengthen_it(self):
        self.fail_by_silence(3)
        until = self.cooldown()["until"]
        dcc_fetch._note_fetch_failure("SomeBot", time.time() + 60)
        self.assertEqual(self.cooldown()["until"], until)
        self.assertNotIn("somebot", dcc_fetch._fetch_failures)


class WhatCountsAsAFailure(CooldownCase):
    def refuse_connections(self):
        fake = types.SimpleNamespace(socket=RefusingSocket, AF_INET=socket.AF_INET,
                                     SOCK_STREAM=socket.SOCK_STREAM, timeout=socket.timeout)
        real = dcc_fetch.socket
        dcc_fetch.socket = fake
        self.addCleanup(setattr, dcc_fetch, "socket", real)

    def connect_and_fail(self, name):
        rid = self.ask("SomeBot", name)
        row = self.row(rid)
        row.update(state="receiving")
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        dcc_fetch._run_transfer(row, {"size": 64, "ip": "127.0.0.1", "port": 9}, dest, f"{name}.flac")
        self.assertTrue(row["reason"].startswith("connect error"))

    def test_an_offer_we_could_not_connect_to_counts(self):
        self.set_config(FETCH_BOT_MAX_FAILS=2)
        self.refuse_connections()
        self.connect_and_fail("One")
        self.connect_and_fail("Two")
        self.assertEqual(self.cooldown()["by"], "cooldown")

    def test_three_failed_connections_still_pause_it_until_resumed(self):
        """#926's pause for a bot we cannot connect to is left as it was: it
        waits for the operator, and a cooldown never shortens it."""
        self.refuse_connections()
        for name in ("One", "Two", "Three"):
            self.connect_and_fail(name)
        entry = self.cooldown()
        self.assertEqual(entry["by"], "auto")
        self.assertNotIn("until", entry)

    def test_a_transfer_that_breaks_off_counts(self):
        self.set_config(FETCH_BOT_MAX_FAILS=1)
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        theirs.sendall(b"x" * 10)
        theirs.close()
        rid = self.ask()
        row = self.row(rid)
        row.update(state="receiving")
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        dcc_fetch._run_transfer(row, {"size": 64, "ip": None, "port": 0}, dest, "Track.flac",
                                sock=TcpLike(ours))
        self.assertEqual(row["state"], "failed")
        self.assertIsNotNone(self.cooldown())

    def test_a_request_left_queued_there_for_too_long_counts(self):
        self.set_config(FETCH_BOT_MAX_FAILS=1)
        rid = self.ask()
        self.row(rid).update(state="queued", queued_at=time.time() - 10 ** 6)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.row(rid)["state"], "failed")
        self.assertIsNotNone(self.cooldown())

    def test_a_refusal_with_a_reason_does_not_count(self):
        rids = [self.ask("SomeBot", f"Missing {n}") for n in range(4)]
        self.asked(*rids)
        for n, rid in enumerate(rids):
            self.assertEqual(dcc_fetch.handle_bot_reply("SomeBot", f"Sorry, but Missing {n}.flac is not found"),
                             "refused")
            self.assertEqual(self.row(rid)["state"], "failed")
        dcc_fetch.check_fetch_queue()
        self.assertIsNone(self.cooldown())
        self.assertNotIn("somebot", dcc_fetch._fetch_failures)

    def test_a_busy_answer_does_not_count(self):
        for n in range(4):
            rid = self.ask("SomeBot", f"Busy {n}")
            self.asked(rid)
            self.row(rid)["busy_retries"] = dcc_fetch.BUSY_RETRIES   # its last ask
            self.assertEqual(dcc_fetch.handle_bot_reply("SomeBot", "Error: The server's global queue is full"),
                             "busy")
            self.assertEqual(self.row(rid)["state"], "failed")
        dcc_fetch.check_fetch_queue()
        self.assertIsNone(self.cooldown())

    def test_a_folder_refused_as_not_packed_here_does_not_count(self):
        self.set_config(FETCH_BOT_MAX_FAILS=1)
        rid = dcc_fetch.enqueue_fetch("SomeBot", "!rar Artist/Album", request_type="folder")
        self.asked(rid)
        self.assertTrue(dcc_fetch.handle_refusal_notice("SomeBot", "Rar Server is currently disabled."))
        self.assertEqual(self.row(rid)["state"], "failed")
        self.assertIsNone(self.cooldown())

    def test_the_operator_s_cancels_do_not_count(self):
        rids = [self.ask("SomeBot", f"Cancelled {n}") for n in range(4)]
        self.asked(*rids)
        for rid in rids:
            self.assertEqual(webserver.build_fetch_delete_result(rid)[0], 200)
        dcc_fetch.check_fetch_queue()
        self.assertIsNone(self.cooldown())
        self.assertNotIn("somebot", dcc_fetch._fetch_failures)

    def test_an_offer_we_turned_down_ourselves_does_not_count(self):
        self.set_config(FETCH_BOT_MAX_FAILS=1, MAX_FETCH_FILE_SIZE=1000)
        rid = self.ask()
        self.asked(rid)
        dcc_fetch.handle_incoming_offer(None, "SomeBot", "DCC SEND Track.flac 2130706433 5000 999999")
        self.assertEqual(self.row(rid)["state"], "failed")
        self.assertIn("exceeds MAX_FETCH_FILE_SIZE", self.row(rid)["reason"])
        self.assertIsNone(self.cooldown())


class ItsRequestsWait(CooldownCase):
    def test_they_wait_saying_until_when_and_are_not_failed(self):
        self.fail_by_silence(3)
        until = self.cooldown()["until"]
        rid = self.ask("SomeBot", "Later")
        for _ in range(3):
            dcc_fetch.check_fetch_queue()
        row = self.row(rid)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["waiting"], "cooldown")
        self.assertEqual(row["cooldown_until"], until)
        self.assertEqual(row["cooldown_failures"], 3)
        self.assertEqual(row["reason"], "")

    def test_another_bot_is_asked_meanwhile(self):
        self.fail_by_silence(3)
        rid = self.ask("OtherBot", "Elsewhere")
        self.asked(rid)

    def test_a_request_out_when_it_began_is_not_asked_again_until_it_ends(self):
        out = self.ask("SomeBot", "Out")
        self.asked(out)
        self.fail_by_silence(3)
        self.row(out)["offered_at"] = time.time() - 10000   # first ask unanswered
        dcc_fetch.check_fetch_queue()
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.row(out)["state"], "pending")
        self.assertEqual(self.row(out)["waiting"], "cooldown")

    def test_a_pause_by_hand_is_told_apart(self):
        dcc_fetch.pause_bot("SomeBot", "testing")
        rid = self.ask()
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.row(rid)["waiting"], "paused")
        self.assertNotIn("cooldown_until", self.row(rid))

    def test_a_cooldown_turned_into_a_pause_by_hand_loses_its_time(self):
        self.fail_by_silence(3)
        rid = self.ask("SomeBot", "Later")
        dcc_fetch.check_fetch_queue()
        self.assertIn("cooldown_until", self.row(rid))
        dcc_fetch.pause_bot("SomeBot", "testing")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.row(rid)["waiting"], "paused")
        self.assertNotIn("cooldown_until", self.row(rid))
        self.assertNotIn("cooldown_failures", self.row(rid))


class ItEndsByItsStoredTime(CooldownCase):
    def time_passes(self, bot="somebot"):
        """The stored end is now in the past, in memory and on disk."""
        dcc_fetch._paused[bot]["until"] = time.time() - 1
        data = self.stored()
        data[bot]["until"] = time.time() - 1
        self.store(data)

    def test_it_is_stored_beside_the_other_pauses(self):
        self.fail_by_silence(3)
        entry = self.stored()["somebot"]
        self.assertEqual(entry["by"], "cooldown")
        self.assertEqual(entry["until"], self.cooldown()["until"])

    def test_the_waiting_requests_go_out_in_order_once_it_is_over(self):
        self.fail_by_silence(3)
        self.set_config(FETCH_MAX_PER_BOT=1)
        rids = [self.ask("SomeBot", f"Queued {n}") for n in range(3)]
        for n, rid in enumerate(rids):
            self.row(rid)["requested_at"] = 1000 + n
        dcc_fetch.check_fetch_queue()
        self.assertTrue(all(self.row(rid)["state"] == "pending" for rid in rids))
        self.time_passes()
        dcc_fetch.check_fetch_queue()
        self.assertEqual([self.row(rid)["state"] for rid in rids], ["offered", "pending", "pending"])
        self.assertEqual(self.row(rids[1])["waiting"], "their-turn")
        self.assertNotIn("cooldown_until", self.row(rids[0]))
        self.assertNotIn("somebot", self.stored(), "its end was saved")

    def test_the_list_of_paused_bots_drops_it_once_its_time_has_come(self):
        """What the dashboard's /api/fetch/paused reads, between two ticks."""
        self.fail_by_silence(3)
        self.time_passes()
        self.assertEqual(dcc_fetch.paused_bots(), {})
        self.assertNotIn("somebot", self.stored())

    def test_it_is_not_over_before_its_time(self):
        self.fail_by_silence(3)
        dcc_fetch._paused["somebot"]["until"] = time.time() + 5
        rid = self.ask("SomeBot", "Later")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.row(rid)["state"], "pending")

    def test_it_survives_a_restart(self):
        self.fail_by_silence(3)
        until = self.cooldown()["until"]
        rid = self.ask("SomeBot", "Later")
        dcc_fetch._paused.clear()           # the process ends
        dcc_fetch._fetch_failures.clear()
        dcc_fetch.load_paused_bots()        # and starts again
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.row(rid)["state"], "pending")
        self.assertEqual(self.row(rid)["cooldown_until"], until)

    def test_one_that_ended_while_the_bot_was_down_is_over_at_start(self):
        self.fail_by_silence(3)
        rid = self.ask("SomeBot", "Later")
        self.time_passes()
        dcc_fetch._paused.clear()
        dcc_fetch.load_paused_bots()
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.row(rid)["state"], "offered")
        self.assertNotIn("somebot", self.stored())

    def test_the_count_starts_again_after_it(self):
        self.fail_by_silence(3)
        self.time_passes()
        self.fail_by_silence(2)
        self.assertIsNone(self.cooldown())
        self.fail_by_silence(1)
        self.assertIsNotNone(self.cooldown())


class ResumeNow(CooldownCase):
    def test_resume_now_ends_it_and_the_requests_go(self):
        self.fail_by_silence(3)
        rid = self.ask("SomeBot", "Later")
        dcc_fetch.check_fetch_queue()
        status, _result = webserver.build_fetch_pause_result({"bot": "SomeBot"}, False)
        self.assertEqual(status, 200)
        self.assertNotIn("somebot", self.stored())
        self.asked(rid)

    def test_the_count_starts_again_after_it(self):
        self.fail_by_silence(2)
        dcc_fetch.pause_bot("SomeBot", "testing")
        dcc_fetch.resume_bot("SomeBot")
        self.fail_by_silence(1)
        self.assertIsNone(self.cooldown())


class WhatThePagesSay(CooldownCase):
    def cooled_row(self):
        self.fail_by_silence(3)
        rid = self.ask("SomeBot", "Later")
        dcc_fetch.check_fetch_queue()
        return rid, self.row(rid)

    def test_the_console_says_until_when_and_after_how_many(self):
        _rid, row = self.cooled_row()
        clock = time.strftime("%H:%M", time.localtime(row["cooldown_until"]))
        self.assertEqual(adminchat._download_waiting_note(row), f"paused until {clock} after 3 failures")

    def test_the_download_queues_row_keeps_its_fields_in_order(self):
        rid, row = self.cooled_row()
        clock = time.strftime("%H:%M", time.localtime(row["cooldown_until"]))
        (line,) = [text for text in adminchat.dlqueue_lines([(rid, row)]) if " DQROW " in text]
        fields = line.split(" ")
        self.assertEqual(fields[:7], ["DCCORE", "DQROW", rid, "f", "pending", "SomeBot",
                                      f"paused_until_{clock}_after_3_failures"])
        self.assertEqual(" ".join(fields[7:]), "Later.flac")

    def test_the_downloads_page_says_it_and_offers_resume_now(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            code = handle.read()
        self.assertIn('if (row.waiting === "cooldown") { label = fetchCooldownLabel(row); }', code)
        branch = code.index('} else if (state === "pending" && row.waiting === "cooldown") {')
        self.assertIn('t("download.fetchCooldownResumeNow")', code[branch:branch + 400])
        self.assertIn("fetch-resume-btn", code[branch:branch + 400])
        for lang in ("en", "es", "fr"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", f"{lang}.json"), encoding="utf-8") as handle:
                strings = json.load(handle)
            self.assertIn("{time}", strings["download.waiting.fetchCooldown"])
            self.assertIn("{failures}", strings["download.waiting.fetchCooldown"])
            self.assertTrue(strings["download.fetchCooldownResumeNow"])

    def test_both_settings_are_on_the_settings_page(self):
        categories = {cid: keys for cid, _title, keys in webserver.SETTINGS_CATEGORIES}
        self.assertIn("FETCH_BOT_MAX_FAILS", categories["fetch-queue"])
        self.assertIn("FETCH_BOT_COOLDOWN_MINUTES", categories["fetch-queue"])


if __name__ == "__main__":
    unittest.main()
