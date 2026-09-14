"""A folder request to a bot with no sign of packing fails fast.

FETCH_FOLDER_OFFER_TIMEOUT is 1800 seconds, and rightly so: the other bot has
to run its own packing pipeline before it can even begin the DCC SEND, and a
real album takes real time.

That allowance is wrong for a bot that never packs anything. There a
non-answer is the EXPECTED outcome rather than a slow one, and the wait is one
of MAX_FETCH_SLOTS - three by default. Half an hour of a third of the fetch
capacity is a heavy price for finding out what the bot's own list already
said.

Measured against one live registry: 2 of 51 known bots publish a RAR folder
list at all.

TWO SIGNALS, EITHER ONE ENOUGH. A RAR list of theirs among the lists we hold -
their own statement of the folders they will pack - or the "@<nick>^ ... RAR
folders" advert irc.py has parsed into known_bots since #133 and which nothing
outside the registry had ever read.

USED TO DECIDE HOW LONG TO WAIT, NEVER WHETHER TO ASK. A bot can pack folders
with neither signal - we may simply never have caught the advert, and may hold
only its main list. Refusing on this would take away something that works.
Waiting less costs nothing when the guess is wrong, and half an hour of a slot
when it is right.

The dashboard now only offers the button where a bot's own list says it will
pack that folder, so a request like this should be rare. This is the net under
what the list cannot cover: a request made through the API, or a bot that has
stopped packing since its list was fetched.
"""

import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "someotherbot"


class RecognisingABotThatPacks(DCCoreTestCase):

    def test_a_bot_we_hold_a_rar_list_from(self):
        """The strongest signal there is: their own list of the folders they
        will pack, in our hands."""
        config.fetched_bot_lists[BOT] = {
            "bot": BOT, "lists": {"": {}, "rar": {}},
        }

        self.assertTrue(list_fetch.bot_publishes_a_rar_list(BOT))

    def test_the_marker_is_matched_without_regard_to_case(self):
        config.fetched_bot_lists[BOT] = {"bot": BOT, "lists": {"": {}, "RAR": {}}}

        self.assertTrue(list_fetch.bot_publishes_a_rar_list(BOT))

    def test_a_bot_we_hold_only_a_plain_list_from(self):
        config.fetched_bot_lists[BOT] = {"bot": BOT, "lists": {"": {}}}

        self.assertFalse(list_fetch.bot_publishes_a_rar_list(BOT))

    def test_a_bot_whose_video_list_we_hold_is_not_a_packer(self):
        """A second list is not a RAR list. Films are not albums."""
        config.fetched_bot_lists[BOT] = {
            "bot": BOT, "lists": {"": {}, "VIDEO": {}},
        }

        self.assertFalse(list_fetch.bot_publishes_a_rar_list(BOT))

    def test_a_bot_that_advertises_a_rar_folder_list(self):
        """"Type @PackBot^ to get my list of 39,454 RAR folders" - parsed since
        #133, and read outside the registry for the first time here."""
        runtime.known_bots[BOT] = {"nick": BOT, "rar_folders": 39454}

        self.assertTrue(list_fetch.bot_publishes_a_rar_list(BOT))

    def test_the_trigger_alone_is_enough(self):
        runtime.known_bots[BOT] = {"nick": BOT, "rar_trigger": "@somebot^"}

        self.assertTrue(list_fetch.bot_publishes_a_rar_list(BOT))

    def test_an_ordinary_advert_is_not(self):
        runtime.known_bots[BOT] = {"nick": BOT, "files": 719041,
                                   "list_date": "2026-09-07"}

        self.assertFalse(list_fetch.bot_publishes_a_rar_list(BOT))

    def test_a_bot_we_have_never_heard_of(self):
        self.assertFalse(list_fetch.bot_publishes_a_rar_list("nosuchbot"))
        self.assertFalse(list_fetch.bot_publishes_a_rar_list(""))
        self.assertFalse(list_fetch.bot_publishes_a_rar_list(None))

    def test_a_malformed_registry_entry_is_not_a_crash(self):
        """This is read on the queue sweep, every tick, inside the lock."""
        runtime.known_bots[BOT] = "not a dict at all"
        config.fetched_bot_lists[BOT] = ["not a dict either"]

        self.assertFalse(list_fetch.bot_publishes_a_rar_list(BOT))


class TheWaitMatchesWhatIsLikelyComing(DCCoreTestCase):

    def timeout_for(self, request_type="folder", bot=BOT):
        return dcc_fetch._offer_timeout_for(
            {"request_type": request_type, "bot": bot},
            offer_timeout=60, folder_timeout=1800, unadvertised_timeout=120)

    def test_a_packer_gets_the_full_wait(self):
        """Packing a real album takes real time on the other end."""
        config.fetched_bot_lists[BOT] = {"bot": BOT, "lists": {"": {}, "rar": {}}}

        self.assertEqual(self.timeout_for(), 1800)

    def test_a_bot_with_no_sign_of_packing_gets_the_short_one(self):
        """The defect: half an hour of one of three fetch slots, spent
        discovering what the bot's own list already said."""
        self.assertEqual(self.timeout_for(), 120)

    def test_a_file_request_is_untouched(self):
        self.assertEqual(self.timeout_for(request_type="file"), 60)

    def test_a_list_request_is_untouched(self):
        self.assertEqual(self.timeout_for(request_type="list"), 60)

    def test_a_failure_deciding_waits_longer_rather_than_less(self):
        """This runs inside the queue lock on every tick. The longer wait is
        the safe way to be wrong: it only ever waits, where the short one
        could give up on a pack that was still coming."""
        real = list_fetch.bot_publishes_a_rar_list
        list_fetch.bot_publishes_a_rar_list = lambda bot: (
            _ for _ in ()).throw(RuntimeError("registry exploded"))
        self.addCleanup(setattr, list_fetch, "bot_publishes_a_rar_list", real)

        self.assertEqual(self.timeout_for(), 1800)


class TheSweepUsesIt(DCCoreTestCase):
    """End to end through check_fetch_queue()'s own expiry pass."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#somechannel", MAX_FETCH_SLOTS=3,
                        FETCH_FOLDER_OFFER_TIMEOUT=1800,
                        FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED=120)

    def stale_folder_row(self, age):
        request_id = dcc_fetch.enqueue_fetch(
            BOT, "!rar Artist/Album", request_type="folder")
        row = config.fetch_queue[request_id]
        row["state"] = "offered"
        row["offered_at"] = time.time() - age
        return request_id

    def test_an_unadvertised_request_is_given_up_on_after_the_short_wait(self):
        request_id = self.stale_folder_row(age=300)

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[request_id]["state"], "failed")

    def test_a_packers_request_is_still_waiting_at_the_same_age(self):
        """The control. Without it, the test above passes just as happily
        against a build that failed every folder request at 300 seconds."""
        config.fetched_bot_lists[BOT] = {"bot": BOT, "lists": {"": {}, "rar": {}}}
        request_id = self.stale_folder_row(age=300)

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[request_id]["state"], "offered")

    def test_a_packers_request_does_eventually_time_out(self):
        config.fetched_bot_lists[BOT] = {"bot": BOT, "lists": {"": {}, "rar": {}}}
        request_id = self.stale_folder_row(age=2000)

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[request_id]["state"], "failed")

    def test_a_young_request_is_left_alone_either_way(self):
        request_id = self.stale_folder_row(age=5)

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[request_id]["state"], "offered")

    def test_the_slot_comes_back(self):
        """The whole point of the shorter wait - MAX_FETCH_SLOTS is three."""
        request_id = self.stale_folder_row(age=300)
        self.assertEqual(dcc_fetch.count_active_fetches(config.fetch_queue), 1)

        dcc_fetch.check_fetch_queue()

        self.assertEqual(dcc_fetch.count_active_fetches(config.fetch_queue), 0)
        self.assertEqual(config.fetch_queue[request_id]["reason"], "no response")


class TheSettingIsOnThePage(unittest.TestCase):

    def test_an_operator_can_change_it(self):
        import webserver

        payload = webserver.build_settings_payload()
        names = [field["name"] for category in payload["categories"]
                 for field in category["fields"]]

        self.assertIn("FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED", names)

    def test_it_sits_with_the_timeout_it_qualifies(self):
        """Reading one without the other tells you half a story."""
        import webserver

        payload = webserver.build_settings_payload()
        for category in payload["categories"]:
            names = [field["name"] for field in category["fields"]]
            if "FETCH_FOLDER_OFFER_TIMEOUT" in names:
                self.assertIn("FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED", names)
                return
        self.fail("FETCH_FOLDER_OFFER_TIMEOUT is not on the page at all")


if __name__ == "__main__":
    unittest.main()
