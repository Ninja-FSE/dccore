""""Sent:" for a private-message request on the direct path was addressed
to the bot's own nick (audit M56, #658).

handle_download_request() handed the raw wire target to start_dcc_send() as
the announce channel on the direct-send path - the common first request: a
slot free, no queue. For a private request that target is the bot's own
nick (admitted on purpose, so a PM can ask for a file), and
send_transfer_complete() built `PRIVMSG <ournick> :Sent ...`: queued into
the VIP lane, a pacer slot spent, and dropped by the read loop as our own
message. The transfer completed and the feed's SENT event fired; only the
public advert line was lost. #530 fixed exactly this for rows picked up
from the queue via announce_channel_for(); the direct path never went
through it, and a PM `!list` request takes the same path.

The direct path now resolves the announce channel the way the queued paths
do: the request's channel if it is one, the configured default otherwise.

#1242 changed both halves of that for a private request. Its channel is the
one the requester shares with the bot, not the first configured one they may
never have been in; and its "Sent:" line goes to no channel at all - the
operator's decision on #1242 - so the line the old third test checked at the
wire is now checked not to be sent.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests.test_path_security import InlineThread  # noqa: E402

BOT = "DCCoreTest"      # ServesARealRequest's NICKNAME


class ADirectSendForAPrivateRequest(feed.ServesARealRequest):

    def started_send_channel(self):
        sends = [args for name, args in InlineThread.dispatched if name == "start_dcc_send"]
        self.assertEqual(len(sends), 1, InlineThread.dispatched)
        return sends[0][4]      # (irc_sock, user, path, file_name, channel, next_file)

    def test_names_the_channel_the_requester_shares_not_the_bot(self):
        self.request("Song.flac", channel=BOT)      # the wire target of a PM is our nick

        self.assertEqual(self.kinds()[-2:], ["REQUEST", "SENDING"])
        channel = self.started_send_channel()
        self.assertTrue(dcc.is_channel_name(channel), "start_dcc_send was handed %r" % channel)
        # dave is in OTHER only; the default is the first configured channel.
        self.assertEqual(channel, feed.OTHER)
        self.assertNotEqual(channel, dcc.default_announce_channel())
        self.assertEqual(self.last("SENDING")["channel"], channel)

    def test_a_channel_request_still_announces_where_it_was_asked(self):
        """The queued paths' rule, on this path: a real channel is kept."""
        self.request("Song.flac", channel=feed.OTHER)

        self.assertEqual(self.started_send_channel(), feed.OTHER)
        self.assertEqual(self.last("SENDING")["channel"], feed.OTHER)

    def test_the_sent_line_reaches_no_channel(self):
        """What send_transfer_complete() is handed for this path's row,
        checked at the wire: nothing goes out, to a channel or to us."""
        import announce
        self.set_config(ANNOUNCE_TRANSFERS=True)
        self.request("Song.flac", channel=BOT)
        sends = [args for name, args in InlineThread.dispatched if name == "start_dcc_send"]
        channel, row = sends[0][4], sends[0][5]
        self.oserve.queued.clear()

        announce.send_transfer_complete(channel, "dave", "Song.flac", 4096, 0.0, 1000, duration=1.0,
                                        private=dcc.is_private_row(row))

        self.assertEqual([m for u, m, *_ in self.oserve.queued if u == "channel_announce"], [])
        self.assertEqual(self.last("SENT")["channel"], feed.OTHER)


for _name in [n for n in dir(feed.ServesARealRequest) if n.startswith("test")]:
    setattr(ADirectSendForAPrivateRequest, _name, None)


if __name__ == "__main__":
    unittest.main()
