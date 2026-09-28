"""A DCCore that is reloading has queued the request, not refused it (#972).

Asked for a file while it rehashes, a DCCore bot keeps the request: "The bot
is reloading its configuration. Your request is queued and starts when the
reload is done." - followed by its usual "Added ... at position". That first
line was read as "busy": our row went back to pending for ten minutes, the
position line found nothing waiting for it, and the file the other bot sent
once its reload was done was refused as one we never asked for.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_fetch_queue_waits_and_paces_itself as paces  # noqa: E402

RELOADING = ("\x02System Message\x0f: The bot is reloading its configuration. "
             "Your request is queued and starts when the reload is done.")
ADDED = "Added Track 00.flac to your personal queue at position #2 of 10."


class WhileItReloads(paces.QueueCase):
    def test_the_request_waits_in_their_queue(self):
        rid = self.queue_up(1)[0]
        dcc_fetch.check_fetch_queue()
        self.assertEqual(dcc_fetch.handle_bot_reply("ServerOne", RELOADING), "queued")
        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "queued")
        self.assertFalse(row.get("retry_at"), "not put off as busy")

        dcc_fetch.handle_bot_reply("ServerOne", ADDED)
        self.assertEqual(row["queue_position"], 2)

    def test_the_file_that_comes_after_the_reload_is_taken(self):
        rid = self.queue_up(1)[0]
        dcc_fetch.check_fetch_queue()
        dcc_fetch.handle_bot_reply("ServerOne", RELOADING)
        with dcc_fetch._fetch_lock():
            claimed = dcc_fetch._claim_matching_offer_locked(config.fetch_queue, "ServerOne", "Track 00.flac")
        self.assertEqual(claimed[0], rid)


if __name__ == "__main__":
    unittest.main()
