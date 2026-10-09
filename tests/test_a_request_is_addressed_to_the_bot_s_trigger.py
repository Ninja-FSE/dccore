"""A request goes to the word the bot answers to, not always to its nick
(#1209).

Every request used to be "@<nick>" for a list and "!<nick> <file>" for a
file. That is right for OmenServe, SPQR and DCCore, whose trigger is their
nick. mxrarserver lets its operator choose any word - "@Music", "!Music
<file>" - and a request addressed to the nick goes unanswered.

The trigger comes from three places, in this order:

  * the line itself, when the operator pasted "!<trigger> <file>";
  * the bot's advert ("Type: @<trigger> to get list(s) of ...");
  * the bot's list we hold, whose rows begin "!<trigger>" - the only place a
    bot in mxrarserver's "request only" mode, which never advertises, says it.

Everything that comes BACK is still matched by the sender's nick: the
channel, the reply, the DCC offer. And a bot with no trigger of its own is
asked exactly as before.

The trigger is kept wherever the bot or the request is kept - the bot
registry, the held lists, the fetch history - so it survives a restart.

Every nick, trigger and channel here is invented.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "SomeServer"
KEY = BOT.lower()
TRIGGER = "SomeTrigger"


class Dispatching(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#somechannel", FETCH_MAX_PER_BOT=10)
        config.channel_users["#somechannel"] = {KEY, "someuser"}
        runtime.known_bots.clear()
        self.addCleanup(runtime.known_bots.clear)

    def asked(self):
        return [msg for _user, msg, *_ in self.oserve.queued if "PRIVMSG" in msg]

    def advertise(self, trigger=TRIGGER):
        runtime.known_bots[KEY] = {"nick": BOT, "trigger": trigger, "files": 10}

    def test_a_list_is_asked_for_with_the_advertised_trigger(self):
        self.advertise()
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), [f"PRIVMSG #somechannel :@{TRIGGER}\r\n"])

    def test_a_file_too(self):
        self.advertise()
        dcc_fetch.enqueue_fetch(BOT, "Some Track.mp3")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), [f"PRIVMSG #somechannel :!{TRIGGER} Some Track.mp3\r\n"])

    def test_a_folder_too(self):
        self.advertise()
        dcc_fetch.enqueue_fetch(BOT, "!rar Artist/Album", request_type="folder")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), [f"PRIVMSG #somechannel :!{TRIGGER} !rar Artist/Album\r\n"])

    def test_a_bot_with_no_trigger_is_asked_by_its_nick_as_before(self):
        runtime.known_bots[KEY] = {"nick": BOT, "files": 10, "rar_trigger": "SomeServer^"}
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")
        dcc_fetch.enqueue_fetch(BOT, "Some Track.mp3")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(sorted(self.asked()), [f"PRIVMSG #somechannel :!{BOT} Some Track.mp3\r\n",
                                                f"PRIVMSG #somechannel :@{BOT}\r\n"])

    def test_the_list_we_hold_says_it_when_no_advert_does(self):
        config.fetched_bot_lists[KEY] = {"bot": BOT, "trigger": TRIGGER}
        dcc_fetch.enqueue_fetch(BOT, "Some Track.mp3")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), [f"PRIVMSG #somechannel :!{TRIGGER} Some Track.mp3\r\n"])

    def test_the_advert_comes_before_the_list(self):
        self.advertise(trigger="NewTrigger")
        config.fetched_bot_lists[KEY] = {"bot": BOT, "trigger": "OldTrigger"}
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), ["PRIVMSG #somechannel :@NewTrigger\r\n"])

    def test_the_pasted_line_comes_before_both(self):
        self.advertise(trigger="NewTrigger")
        dcc_fetch.enqueue_fetch(BOT, "Some Track.mp3", trigger="PastedTrigger")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), ["PRIVMSG #somechannel :!PastedTrigger Some Track.mp3\r\n"])

    def test_a_trigger_that_cannot_be_sent_is_not_used(self):
        runtime.known_bots[KEY] = {"nick": BOT, "trigger": "two words"}
        config.fetched_bot_lists[KEY] = {"bot": BOT, "trigger": "bad\r\nQUIT"}
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), [f"PRIVMSG #somechannel :@{BOT}\r\n"])

    def test_what_may_be_sent(self):
        import irc
        self.assertTrue(irc.is_sendable_trigger(TRIGGER))
        self.assertTrue(irc.is_sendable_trigger("Some^Bot|away"))
        for bad in ("", "two words", "line\n", "line\r", "a,b", "x" * 65, None, 12):
            with self.subTest(bad=bad):
                self.assertFalse(irc.is_sendable_trigger(bad))

    def test_a_pasted_trigger_that_cannot_be_sent_is_not_kept(self):
        rid = dcc_fetch.enqueue_fetch(BOT, "Some Track.mp3", trigger="bad\nword")

        self.assertNotIn("trigger", config.fetch_queue[rid])

    def test_the_offer_and_the_reply_are_still_matched_by_the_nick(self):
        self.advertise()
        rid = dcc_fetch.enqueue_fetch(BOT, "Some Track.mp3")
        dcc_fetch.check_fetch_queue()

        dcc_fetch.handle_bot_reply(BOT, "Request accepted: Some Track.mp3 | Queue position: 2 | "
                                        "Allowed requests: 1 of 5")
        self.assertEqual(config.fetch_queue[rid]["state"], "queued")
        with dcc_fetch._fetch_lock():
            claimed = dcc_fetch._claim_matching_offer_locked(
                config.fetch_queue, TRIGGER, "Some_Track.mp3")
        self.assertEqual(claimed, (None, None))
        with dcc_fetch._fetch_lock():
            claimed = dcc_fetch._claim_matching_offer_locked(
                config.fetch_queue, BOT, "Some_Track.mp3")
        self.assertEqual(claimed[0], rid)


class APastedLine(DCCoreTestCase):
    """The Download tab's paste box splits "!<word> <file>" into a bot and a
    file. For an mxrarserver list that word is the trigger."""

    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, fetch_feature_disabled=False, CHANNEL="#somechannel")
        config.channel_users["#somechannel"] = {KEY, "someuser"}
        runtime.known_bots.clear()
        self.addCleanup(runtime.known_bots.clear)

    def paste(self, word, filename="Some Track.mp3"):
        return webserver.build_fetch_enqueue_result([{"bot": word, "filename": filename}])

    def test_a_trigger_resolves_to_its_bot(self):
        runtime.known_bots[KEY] = {"nick": BOT, "trigger": TRIGGER}

        status, body = self.paste(TRIGGER)

        self.assertEqual(status, 200, body)
        row = config.fetch_queue[body["created"][0]]
        self.assertEqual((row["bot"], row["trigger"]), (BOT, TRIGGER))

    def test_case_does_not_matter(self):
        runtime.known_bots[KEY] = {"nick": BOT, "trigger": TRIGGER}

        status, body = self.paste(TRIGGER.lower())

        self.assertEqual(config.fetch_queue[body["created"][0]]["bot"], BOT)

    def test_a_held_list_s_trigger_resolves_too(self):
        config.fetched_bot_lists[KEY] = {"bot": BOT, "trigger": TRIGGER}

        status, body = self.paste(TRIGGER)

        self.assertEqual(config.fetch_queue[body["created"][0]]["bot"], BOT)

    def test_a_nick_we_know_stays_that_nick(self):
        """A bot whose nick is the word is asked, even if another bot's
        trigger is the same word."""
        runtime.known_bots["otherserver"] = {"nick": "OtherServer", "trigger": BOT}
        runtime.known_bots[KEY] = {"nick": BOT, "files": 10}

        status, body = self.paste(BOT)

        row = config.fetch_queue[body["created"][0]]
        self.assertEqual(row["bot"], BOT)
        self.assertNotIn("trigger", row)

    def test_a_trigger_two_bots_share_is_not_guessed(self):
        runtime.known_bots[KEY] = {"nick": BOT, "trigger": TRIGGER}
        runtime.known_bots["otherserver"] = {"nick": "OtherServer", "trigger": TRIGGER}

        status, body = self.paste(TRIGGER)

        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})

    def test_an_unknown_word_is_refused_as_before(self):
        status, body = self.paste("NoSuchWord")

        self.assertEqual(status, 400)
        self.assertEqual(config.fetch_queue, {})


class AfterARestart(DCCoreTestCase):

    def test_the_advertised_trigger_is_in_the_saved_registry(self):
        db.save_known_bots({KEY: {"nick": BOT, "trigger": TRIGGER}})

        self.assertEqual(db.load_known_bots()[KEY]["trigger"], TRIGGER)

    def test_a_pasted_trigger_is_kept_on_the_request(self):
        self.set_config(fetch_queue={})
        rid = dcc_fetch.enqueue_fetch(BOT, "Some Track.mp3", trigger=TRIGGER)
        config.fetch_queue[rid].update(state="offered", offered_at=time.time())

        with dcc_fetch._fetch_lock():
            dcc_fetch._persist_fetch_history_locked(config.fetch_queue)
        restored = db.load_fetch_history()[rid]

        self.assertEqual((restored["state"], restored["trigger"]), ("pending", TRIGGER))
        self.assertEqual(dcc_fetch.request_trigger(BOT, restored), TRIGGER)


class TheListWeHold(DCCoreTestCase):
    """An mxrarserver list's rows begin "!<trigger>"; the trigger is kept with
    the list, and saved with it."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-trigger-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        config.FETCHED_FILES_DIR = self.tmp

    def fetch(self, members):
        path = os.path.join(self.tmp, "incoming.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in members:
                archive.writestr(name, text)
        ok, reason = list_fetch.process_fetched_list_zip(BOT, path)
        self.assertTrue(ok, reason)
        return config.fetched_bot_lists[KEY]

    def mx_list(self, trigger=TRIGGER):
        return ("=" * 40 + "\n  mx.rarserver\n" + "=" * 40 + "\n> Overview\n\n"
                f"!{trigger} Some Track.mp3 ::INFO:: 4.5 MB\n"
                f"!{trigger} Other Track.mp3 ::INFO:: 3.1 MB\n")

    def test_an_mxrarserver_list_keeps_its_trigger(self):
        entry = self.fetch([("SomeServer-Files(2)-MX.txt", self.mx_list())])

        self.assertEqual(entry["trigger"], TRIGGER)
        self.assertEqual(db.load_fetched_bot_lists()[KEY]["trigger"], TRIGGER)

    def test_a_folders_list_in_a_complete_archive_counts(self):
        entry = self.fetch([
            ("SomeServer-Files(2)-MX.txt", self.mx_list()),
            ("SomeServer-Folders(1)-MX.txt",
             f"!{TRIGGER} E:\\Music\\Artist\\Album.rar\n"),
        ])

        self.assertEqual(entry["trigger"], TRIGGER)

    def test_any_other_list_keeps_none(self):
        """An OmenServe list's rows carry the nick it was BUILT under; a bot
        that has changed nick since is still asked by the one it has now."""
        entry = self.fetch([("SomeServer-Default(2026-01-02)-OS.txt",
                             "!OldNick Some Track.mp3 ::INFO:: 4.5 MB\n")])

        self.assertNotIn("trigger", entry)

    def test_a_row_trigger_that_cannot_be_sent_is_not_kept(self):
        long_word = "x" * 80
        entry = self.fetch([("SomeServer-MX.txt", self.mx_list(trigger=long_word))])

        self.assertNotIn("trigger", entry)


if __name__ == "__main__":
    unittest.main()
