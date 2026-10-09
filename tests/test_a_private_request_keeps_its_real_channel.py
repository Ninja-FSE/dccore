"""A private request was reported, moded and announced as the first configured
channel (#1242).

A file asked for by private message stored the bot's own nick as its queue
row's channel. When the send started, announce_channel_for() saw a nick and
fell back to the FIRST configured channel - one the requester may never have
been in. That channel then reached three things: the feed (SENDING, SENT and
QUEUED said "in #first-channel"), the channel-mode check of #1204 (the notices
followed #first-channel's mode, so the private-message rule in
library.mode_for_request() never ran), and the ANNOUNCE_TRANSFERS "Sent:" line.
And somebody in none of our channels was served at once on an idle bot, but
frozen and dropped from the queue on a busy one.

The maintainer's decision on #1242, as built:

1. A file request sent as a private message is not a message: it never
   reaches the dashboard's Messages page, by the same test the dispatcher
   serves requests by.
2. The requester must share a channel with the bot, checked when the request
   arrives - the same answer whether a slot is free or not. Sharing none, it
   is refused silently, as a request in a channel this bot does not serve is;
   the operator sees a console line.
3. Nothing about a private request is announced in any channel, though the
   channel it belongs to is now known.
4. The operator sees the real channel: the feed (console, @DCCore window,
   debug channel) names the channel the requester shares with the bot, and
   that channel's mode decides the notices.

The shared channel is resolved once, when the request is accepted, and kept on
the row with a "private" flag. A row queued before this has neither, and our
own nick for a channel; it still loads, and is resolved the same way when its
turn comes.
"""

import contextlib
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import webserver  # noqa: E402

from tests.support import RecordingSocket, no_disk_writes, silence_debug  # noqa: E402
from tests.test_path_security import InlineThread  # noqa: E402
from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests import test_every_transfer_ending_is_recorded_with_how_it_ended as ending  # noqa: E402
from tests import test_file_requests_really_skip_the_flood_gate as flood  # noqa: E402
from tests.test_irc_dispatch import ALIAS_CONDITION, _evaluate  # noqa: E402

BOT = "SomeBot"
USER = "SomeUser"
FIRST = "#first-channel"
OTHER = "#other-channel"
SONG = "Some Song.mp3"
FOLDER = "!rar Some Band/Some Album"
SWEEP = "system_next_trigger_fallback"


class APrivateRequest(feed.ListensToTheFeed):
    """One track at the library's root and one album, two configured
    channels, SomeUser in #other-channel only, and dcc's threads recorded
    rather than started."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        with io.open(os.path.join(self.tree.music, SONG), "wb") as handle:
            handle.write(b"\x00" * 4096)
        album = os.path.join(self.tree.music, "Some Band", "Some Album")
        os.makedirs(album)
        with io.open(os.path.join(album, "01 Some Song.flac"), "wb") as handle:
            handle.write(b"\x00" * 4096)
        self.lists_file = os.path.join(self.tree.root, "lists.json")
        self.set_config(NICKNAME=BOT, ORIGINAL_NICK=BOT, LIST_BASE_NAME=BOT,
                        CHANNEL="%s,%s" % (FIRST, OTHER), DEBUG_CHANNEL="",
                        FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        LISTS_FILE=self.lists_file, RAR_ENABLED=True,
                        search_inprogress=False, update_inprogress=False,
                        bot_joined_channel=True, ANNOUNCE_TRANSFERS=True)
        no_disk_writes(db)
        self.debug = silence_debug(announce)
        InlineThread.dispatched = []
        real_thread = dcc.threading.Thread
        dcc.threading.Thread = InlineThread
        self.addCleanup(setattr, dcc.threading, "Thread", real_thread)
        config.channel_users[FIRST] = {"someoneelse"}
        config.channel_users[OTHER] = {USER.lower()}
        self.sock = RecordingSocket()

    def bind_lists(self, *entries):
        with io.open(self.lists_file, "w", encoding="utf-8") as handle:
            json.dump(list(entries), handle)

    def main_list(self, channels=(FIRST, OTHER), modes=None, **over):
        entry = {"name": "Main", "primary": True, "channels": list(channels),
                 "folders": [{"name": "Music", "path": self.tree.music}]}
        if modes:
            entry["modes"] = modes
        entry.update(over)
        return entry

    def request(self, what=SONG, target=BOT, user=USER):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dcc.handle_download_request(self.sock, user, what, target)
        return out.getvalue()

    def pick_up(self, who=USER):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.check_queue_and_send(self.sock, who)

    def fill_the_slots(self):
        self.set_config(MAX_DCC_SLOTS=1)
        config.active_transfers.append({"user": "someoneelse", "file": "X.mp3", "bytes_sent": 0})

    def free_the_slots(self):
        config.active_transfers.clear()

    def sends(self):
        return [args for name, args in InlineThread.dispatched if name == "start_dcc_send"]

    def rows(self, user=USER):
        return config.dcc_queue.get(user.lower(), [])

    def notices_to(self, user=USER):
        return [m for _u, m, *_ in self.oserve.queued if m.startswith("NOTICE %s " % user)]


class TheFeedNamesTheChannelTheyShare(APrivateRequest):

    def test_a_send_that_starts_at_once(self):
        self.request()

        self.assertEqual(self.kinds(), ["REQUEST", "SENDING"])
        self.assertEqual(self.last("REQUEST")["channel"], OTHER)
        self.assertEqual(self.last("SENDING")["channel"], OTHER)
        (args,) = self.sends()
        self.assertEqual(args[4], OTHER, "start_dcc_send was handed the wrong channel")
        self.assertIs(args[5].get("private"), True)

    def test_the_debug_channel_and_console_say_it_was_private_and_where(self):
        self.request()

        self.assertIn(("REQUEST", 'SomeUser asked for "Some Song.mp3" by private message (in #other-channel)'),
                      self.debug)

    def test_a_queued_request_and_its_send_later(self):
        self.fill_the_slots()
        self.request()
        self.assertEqual(self.last("QUEUED")["channel"], OTHER)
        (row,) = self.rows()
        self.assertEqual((row["channel"], row.get("private")), (OTHER, True))

        self.free_the_slots()
        self.pick_up()

        self.assertEqual(self.last("SENDING")["channel"], OTHER)
        self.assertEqual(self.sends()[-1][4], OTHER)

    def test_the_global_sweep_names_it_too(self):
        self.fill_the_slots()
        self.request()
        self.free_the_slots()

        self.pick_up(SWEEP)

        self.assertEqual(self.last("SENDING")["channel"], OTHER)

    def test_a_folder_asked_for_privately(self):
        self.request(FOLDER)

        (row,) = self.rows()
        self.assertEqual((row["channel"], row.get("private")), (OTHER, True))
        self.assertEqual(self.last("REQUEST")["channel"], OTHER)
        self.assertEqual(self.last("QUEUED")["channel"], OTHER)
        self.assertIn("by private message (in #other-channel)",
                      [text for kind, text in self.debug if kind == "REQUEST"][-1])

    def test_a_bound_channel_is_the_one_named(self):
        """With lists binding channels, the first bound channel they share."""
        self.bind_lists(self.main_list())
        self.request()

        self.assertEqual(self.last("SENDING")["channel"], OTHER)


class TheNoticesFollowThatChannelsMode(APrivateRequest):
    """Normal against Request only, both ways round: the mode that counts is
    the shared channel's, never the first configured one's."""

    def first_is_request_only(self):
        self.bind_lists(self.main_list(modes={FIRST: "request_only"}))

    def other_is_request_only(self):
        self.bind_lists(self.main_list(modes={OTHER: "request_only"}))

    def test_a_send_at_once_is_told_when_their_channel_is_normal(self):
        self.first_is_request_only()
        self.request()

        self.assertEqual(len(self.notices_to()), 1, self.oserve.queued)

    def test_a_send_at_once_is_not_told_when_their_channel_is_request_only(self):
        self.other_is_request_only()
        self.request()

        self.assertEqual(self.sends()[0][4], OTHER, "the request was not served")
        self.assertEqual(self.notices_to(), [])

    def test_a_queued_send_is_told_when_their_channel_is_normal(self):
        self.first_is_request_only()
        self.fill_the_slots()
        self.request()
        self.assertEqual(len(self.notices_to()), 1, "the queue position")

        self.free_the_slots()
        self.oserve.queued.clear()
        self.pick_up()

        self.assertEqual(len(self.notices_to()), 1, "the Sending notice")

    def test_a_queued_send_is_not_told_when_their_channel_is_request_only(self):
        """The reported case: the pickup took #first-channel's Normal and
        told a requester whose own channel is Request only."""
        self.other_is_request_only()
        self.fill_the_slots()
        self.request()
        self.free_the_slots()
        self.pick_up()

        self.assertEqual(self.last("SENDING")["channel"], OTHER)
        self.assertEqual(self.notices_to(), [])


class SomebodyInNoneOfOurChannelsIsRefused(APrivateRequest):
    """Refused the same way whether a slot is free or not, and silently: no
    notice, no row, no send, no feed event. The console says so."""

    def setUp(self):
        super().setUp()
        config.channel_users[OTHER] = {"someoneelse"}

    def assert_refused(self, printed):
        self.assertEqual(self.events, [])
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.notices_to(), [])
        self.assertIn("none of the channels this bot serves", printed)
        self.assertTrue(any("none of the channels this bot serves" in text for _k, text in self.debug))

    def test_with_a_slot_free(self):
        self.assert_refused(self.request())

    def test_with_the_slots_full(self):
        self.fill_the_slots()
        self.assert_refused(self.request())

    def test_a_folder(self):
        self.assert_refused(self.request(FOLDER))

    def test_the_list(self):
        """"Preparing full list" would promise a list that never comes."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            list_mod.send_file_list(self.sock, USER, BOT)
        self.assert_refused(out.getvalue())

    def test_the_debug_channel_is_not_one_of_ours_to_serve(self):
        self.set_config(DEBUG_CHANNEL="#debug-channel")
        config.channel_users["#debug-channel"] = {USER.lower()}
        self.assert_refused(self.request())

    def test_nor_is_a_channel_no_list_serves(self):
        """The primary binds #first-channel only, so #other-channel is not
        served: a request typed there would not be answered either."""
        self.bind_lists(self.main_list(channels=(FIRST,)))
        config.channel_users[OTHER] = {USER.lower()}
        self.assert_refused(self.request())

    def test_and_joining_one_is_all_it_takes(self):
        config.channel_users[FIRST].add(USER.lower())
        self.request()

        self.assertEqual(self.sends()[0][4], FIRST)


class AChannelRequestIsUnchanged(APrivateRequest):

    def test_its_row_is_as_it_was(self):
        self.fill_the_slots()
        self.request(target=OTHER)

        (row,) = self.rows()
        self.assertEqual(sorted(row), ["channel", "file", "is_temporary_zip", "path", "queued_at", "user_raw"])
        self.assertEqual(row["channel"], OTHER)

    def test_its_send_at_once_is_as_it_was(self):
        self.request(target=OTHER)

        (args,) = self.sends()
        self.assertEqual(args[4], OTHER)
        self.assertNotIn("private", args[5])
        self.assertIn(("REQUEST", 'SomeUser asked for "Some Song.mp3"'), self.debug)

    def test_it_is_served_without_the_presence_check(self):
        """Not seen in any channel yet (NAMES not in): a channel request is
        answered as before. The new check is for private requests."""
        config.channel_users.clear()
        self.request(target=FIRST)

        self.assertEqual(self.sends()[0][4], FIRST)

    def test_a_folder_row_is_as_it_was(self):
        self.request(FOLDER, target=FIRST)

        (row,) = self.rows()
        self.assertEqual(row["channel"], FIRST)
        self.assertNotIn("private", row)


class AnOldQueueRowStillLoadsAndSends(APrivateRequest):
    """dcc_queue.txt written before #1242: a private row has our nick for a
    channel and no flag."""

    def load(self, rows):
        path = os.path.join(self.tree.root, "dcc_queue.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        with mock.patch.object(db, "DCC_QUEUE_FILE", path), contextlib.redirect_stdout(io.StringIO()):
            db.load_dcc_queue()

    def old_row(self, user, channel):
        return {"file": SONG, "path": os.path.join(self.tree.music, SONG), "channel": channel,
                "user_raw": user, "is_temporary_zip": False, "queued_at": time.time() - 60}

    def test_a_private_one_loads_and_is_sent_in_the_channel_they_share(self):
        row = self.old_row(USER, BOT)
        self.load({USER.lower(): [row]})
        self.assertEqual(self.rows(), [row])

        self.pick_up()

        self.assertEqual(self.last("SENDING")["channel"], OTHER)
        args = self.sends()[-1]
        self.assertEqual(args[4], OTHER)
        self.assertTrue(dcc.is_private_row(args[5]))

    def test_a_channel_one_loads_and_is_sent_as_before(self):
        config.channel_users[FIRST].add("otheruser")
        self.load({"otheruser": [self.old_row("OtherUser", FIRST)]})

        self.pick_up("OtherUser")

        self.assertEqual(self.last("SENDING")["channel"], FIRST)
        self.assertFalse(dcc.is_private_row(self.sends()[-1][5]))


class NothingIsAnnounced(ending.SendCase):
    """A real send against fake sockets, to the end: ANNOUNCE_TRANSFERS is on
    and the channel is Normal, and a private request still tells no channel."""

    def setUp(self):
        super().setUp()
        self.set_config(ANNOUNCE_TRANSFERS=True, NICKNAME=BOT, CHANNEL="%s,%s" % (FIRST, OTHER))

    def send_row(self, row):
        path = self.served(SONG)
        row = dict(row, path=path, file=SONG)
        ending.FakeListener.receiver = ending.FakeReceiver("complete")
        config.active_transfers[:] = [{"user": USER, "file": SONG, "bytes_sent": 0, "next_file_obj": SONG}]
        irc_sock = ending.RecordingIrcSocket()
        self.oserve.irc_connection = irc_sock
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.start_dcc_send(irc_sock, USER, path, SONG, dcc.announce_channel_for(row), row)
        return [m for u, m, *_ in self.oserve.queued if u == "channel_announce"]

    def test_a_private_request(self):
        config.channel_users[OTHER] = {USER.lower()}
        self.assertEqual(self.send_row({"channel": OTHER, "private": True, "user_raw": USER}), [])

    def test_a_private_request_queued_before_the_flag(self):
        config.channel_users[OTHER] = {USER.lower()}
        self.assertEqual(self.send_row({"channel": BOT, "user_raw": USER}), [])

    def test_THE_CONTROL_a_channel_request_is_announced_where_it_was_asked(self):
        lines = self.send_row({"channel": OTHER, "user_raw": USER})

        self.assertEqual(len(lines), 1, self.oserve.queued)
        self.assertTrue(lines[0].startswith("PRIVMSG %s :" % OTHER), lines[0])


class TheSharedChannelRule(APrivateRequest):
    """library.shared_channel(), and mode_for_request() reading through it."""

    def test_a_list_bound_channel_comes_before_an_earlier_unbound_one(self):
        self.bind_lists(self.main_list(channels=()),
                        {"name": "Other", "primary": False, "channels": [OTHER],
                         "modes": {OTHER: "quiet"},
                         "folders": [{"name": "Music", "path": self.tree.music}]})
        config.channel_users.clear()
        config.channel_users[FIRST] = {USER.lower()}
        config.channel_users[OTHER] = {USER.lower()}

        self.assertEqual(library.shared_channel(USER), OTHER)
        self.assertEqual(library.mode_for_request(BOT, USER), library.QUIET)

    def test_with_no_bindings_any_channel_of_ours_they_are_in(self):
        self.assertEqual(library.shared_channel(USER), OTHER)
        self.assertEqual(library.mode_for_request(BOT, USER), library.NORMAL)

    def test_a_status_prefix_does_not_hide_them(self):
        config.channel_users[OTHER] = {"@" + USER.lower()}
        self.assertEqual(library.shared_channel(USER), OTHER)

    def test_none(self):
        self.assertIsNone(library.shared_channel("nobody"))
        self.assertIsNone(library.shared_channel(""))
        self.assertEqual(library.mode_for_request(BOT, "nobody"), library.NORMAL)

    def test_the_private_mode_is_the_shared_channels_mode(self):
        """The two can never disagree: the row's channel is what the notices
        read the mode of, where the request itself read the private rule."""
        for modes in ({}, {FIRST: "request_only"}, {OTHER: "request_only"}, {OTHER: "quiet"}):
            for where in ((FIRST,), (OTHER,), (FIRST, OTHER)):
                with self.subTest(modes=modes, where=where):
                    self.bind_lists(self.main_list(modes=modes))
                    config.channel_users.clear()
                    for chan in where:
                        config.channel_users[chan] = {USER.lower()}
                    self.assertEqual(library.mode_for_request(BOT, USER),
                                     library.channel_mode(library.shared_channel(USER)))


class AFileRequestIsNotAMessage(APrivateRequest):
    """Rule 1: the Messages page lists what nobody answered. A file asked for
    by private message is answered."""

    def setUp(self):
        super().setUp()
        announce._pm_last_recorded.clear()

    def record(self, nick, text):
        with contextlib.redirect_stdout(io.StringIO()):
            return announce.record_private_message(nick, text)

    def texts(self):
        return [m["text"] for m in webserver.build_messages_payload()["messages"]]

    def test_a_request_is_not_recorded_and_a_message_is(self):
        self.assertIsNone(self.record(USER, "!SomeBot Some Song.mp3"))
        self.record(USER, "are you there?")

        self.assertEqual(self.texts(), ["are you there?"])
        self.assertEqual(webserver.build_messages_payload()["unread"], 1)

    def test_on_any_name_the_bot_answers_requests_to(self):
        self.set_config(NICKNAME=BOT + "_", ORIGINAL_NICK=BOT)
        self.assertIsNone(self.record(USER, "!somebot Some Song.mp3"))
        self.assertIsNone(self.record("OtherUser", "!SomeBot_ !rar Some Band/Some Album"))
        self.assertEqual(self.texts(), [])

    def test_a_line_that_only_looks_like_one_is_kept(self):
        """Asking ANOTHER bot, or naming ours without a file, is not a request
        to this one - the dispatcher would not serve it either."""
        self.record(USER, "!SomeOtherBot Some Song.mp3")
        self.record("OtherUser", "!SomeBot")

        self.assertEqual(self.texts(), ["!SomeBot", "!SomeOtherBot Some Song.mp3"])

    def test_the_test_is_the_one_the_dispatcher_serves_by(self):
        corpus = ["!SomeBot Some Song.mp3", "!somebot x", "!SomeBot", "!SomeBotX y", "SomeBot y",
                  " !SomeBot y", "!SomeOtherBot y", "!SomeBot !rar Some Band/Some Album", "@SomeBot"]
        for msg in corpus:
            with self.subTest(msg=msg):
                self.assertEqual(irc.names_a_file_request(msg.lower()), _evaluate(ALIAS_CONDITION, msg))


class AFileRequestByPrivateMessageNeverReachesThePage(flood.DrivesFileRequests):
    """The read loop for real: a private request is dispatched, a private
    message is recorded, and the page shows only the message."""

    def setUp(self):
        super().setUp()
        announce._pm_last_recorded.clear()
        self.set_config(PRIVATE_MESSAGES_ENABLED=True)

    def private_line(self, text, nick=USER):
        return ":%s!~u@%s.example PRIVMSG %s :%s" % (nick, nick.lower(), flood.NICK, text)

    def test_only_the_message_is_on_the_page(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.after_001(self.private_line("!%s %s" % (flood.NICK, SONG)),
                           self.private_line("are you there?"))

        self.assertEqual(len(self.download_dispatches()), 1)
        payload = webserver.build_messages_payload()
        self.assertEqual([m["text"] for m in payload["messages"]], ["are you there?"])
        self.assertEqual(payload["unread"], 1)


if __name__ == "__main__":
    unittest.main()
