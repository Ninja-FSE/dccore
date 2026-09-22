"""The pause notice told the user their request "is not lost" while the
request was dropped (audit L4, #668).

During a rehash quiesce (config.transfers_paused, held for up to
REHASH_TRANSFER_WAIT per rehash, and several dashboard saves queue several
waits back to back) handle_download_request() sent "The bot is reloading
its configuration. Your request is not lost - try again in a moment." and
returned without queuing anything. Nothing replayed it after
resume_transfers(). The request WAS lost unless the user typed it again;
they had been told to wait, so they did.

Only the dispatch has to wait. The request is queued now - the direct send
checks the pause under queue_lock, check_queue_and_send() was gated already
- and the rehash's wake after the reload looks at every slot, so a request
made during the pause is served then. The notice says so.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests import test_the_feed_says_which_channel as feed  # noqa: E402

OTHER, SERVED = feed.OTHER, feed.SERVED


class ARequestDuringTheQuiesce(feed.ServesARealRequest):

    def setUp(self):
        super().setUp()
        self.set_config(transfers_paused=True)

    def notices(self):
        return [m for _u, m, *_ in self.oserve.queued if "System Message" in m]

    def test_a_file_request_is_queued_rather_than_sent_or_dropped(self):
        """The slots are free and dave has nothing queued - the shape that
        sends at once - and the pause still puts the row in the queue."""
        self.request("Song.flac", channel=OTHER)

        self.assertEqual([r["file"] for r in config.dcc_queue.get("dave", [])], ["Song.flac"])
        self.assertEqual(config.active_transfers, [], "a send started in the middle of a rehash")
        self.assertNotIn("SENDING", self.kinds())
        self.assertEqual(self.last("QUEUED")["name"], "Song.flac")

    def test_a_folder_request_is_queued_too(self):
        self.request("!rar Metallica/Black Album (1991)", channel=SERVED)

        rows = config.dcc_queue.get("dave", [])
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0].get("is_unpacked_rar_folder"))
        self.assertNotIn("SENDING", self.kinds())

    def test_the_notice_says_the_request_is_queued_and_when_it_starts(self):
        self.request("Song.flac", channel=OTHER)

        told = self.notices()
        self.assertEqual(len(told), 1, told)
        self.assertIn("reloading its configuration", told[0])
        self.assertIn("queued and starts when the reload is done", told[0])
        self.assertNotIn("try again", told[0])
        self.assertNotIn("not lost", told[0])

    def test_the_queued_request_is_served_once_the_pause_is_lifted(self):
        """What "not lost" has to mean: after resume_transfers() the wake the
        rehash runs - wake_restored_queues(), one look per slot - starts the
        send, with nobody typing anything again."""
        self.request("Song.flac", channel=OTHER)
        self.assertNotIn("SENDING", self.kinds())

        dcc.resume_transfers()
        self.quietly(dcc.wake_restored_queues)

        self.assertEqual(self.last("SENDING")["name"], "Song.flac")
        self.assertEqual(self.last("SENDING")["channel"], OTHER)
        # The send itself is a recorded thread here (the row leaves the
        # queue in start_dcc_send's own finally); what was dispatched is it.
        started = [args for name, args in feed.InlineThread.dispatched if name == "start_dcc_send"]
        self.assertEqual([args[1] for args in started], ["dave"])

    def test_two_users_held_by_one_rehash_are_both_served_by_its_wake(self):
        """One pass dispatches one user and breaks; the rehash used to run
        one pass. With requests queued during the pause rather than refused,
        the wake has to look once per free slot."""
        config.channel_users[OTHER].add("erin")
        self.request("Song.flac", channel=OTHER, user="dave")
        self.request("Song.flac", channel=OTHER, user="erin")
        self.assertEqual(sorted(config.dcc_queue), ["dave", "erin"])

        dcc.resume_transfers()
        self.quietly(dcc.wake_restored_queues)

        self.assertEqual(sorted(tx["user"] for tx in config.active_transfers), ["dave", "erin"])

    def test_the_console_line_says_queued_not_held(self):
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            dcc.handle_download_request(self.sock, "dave", "Song.flac", OTHER)

        self.assertIn("[MAINTENANCE] Queued a file request from dave for after the rehash", out.getvalue())
        self.assertNotIn("Held a file request", out.getvalue())


class TheRehashWake(unittest.TestCase):

    def test_the_rehash_wakes_every_slot_not_one_pass(self):
        body = feed.source("commands.py")
        start = body.index("[REHASH-WAKE]")
        wake = body[start:start + 700]

        self.assertIn("target=dcc.wake_restored_queues", wake)
        self.assertNotIn("system_next_trigger_fallback", wake)


for _name in [n for n in dir(feed.ServesARealRequest) if n.startswith("test")]:
    setattr(ARequestDuringTheQuiesce, _name, None)


if __name__ == "__main__":
    unittest.main()
