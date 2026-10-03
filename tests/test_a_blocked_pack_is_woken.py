"""A folder pack turned away while every slot was busy gets its turn (#1034).

Only the specific-user path of check_queue_and_send() can start a pack, and
every caller hands it the nick that just finished - so a pack turned away at
[DCC-BLOCK] was never tried again. The sweep skipped it, and it stayed queued
until its owner's next transfer ended, which for a nick with only the pack is
never. Now the sweep, reaching the pack in wait order with a slot free, starts
its owner's own dispatch; and a pack that has waited longer is a nick worth
yielding to, as a plain file is (#1032) - while no other pack is being made.

Threads that would run check_queue_and_send are recorded, not run (see
InlineThread), so the tests read who was woken.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
from tests.support import queue_row  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_longest_waiting_nick_gets_the_slot as fair  # noqa: E402
from tests import test_the_feed_says_which_channel as feed  # noqa: E402


def a_pack(user):
    return queue_row(user=user, filename="Album.rar", is_unpacked_rar_folder=True, is_temporary_zip=True)


class APackGetsItsTurn(fair.Case):
    def woken(self):
        return [args[1] for name, args in feed.InlineThread.dispatched if name == "check_queue_and_send"]

    def test_a_blocked_pack_is_woken(self):
        """The issue: dave was sending, erin's pack was turned away. dave
        finishes with nothing more, and the slot goes to erin's pack."""
        self.request("A.flac", user="dave")
        config.dcc_queue["erin"] = [a_pack("erin")]
        self.pick_up("erin")                       # turned away: the slot is dave's
        self.assertEqual(self.woken(), [])
        self.finish("dave")
        self.pick_up("dave")
        self.assertEqual(self.woken(), ["erin"])
        self.assertEqual(self.running(), [], "the slot is left for the pack")

    def test_a_pack_that_waited_longer_comes_before_the_finishing_nicks_next_file(self):
        self.request("A.flac", user="dave")
        self.request("B.flac", user="dave")
        config.dcc_queue["erin"] = [a_pack("erin")]
        runtime.queue_waiting_since["erin"] = 1.0
        self.finish("dave")
        self.pick_up("dave")
        self.assertEqual(self.woken(), ["erin"])
        self.assertEqual(self.running(), [], "the one slot is kept for the pack")
        self.assertEqual(self.queued("dave"), ["B.flac"], "dave waits his turn")

    def test_a_plain_file_that_waited_longer_still_comes_first(self):
        config.dcc_queue["frank"] = [queue_row(user="frank", filename="C.flac")]
        config.dcc_queue["erin"] = [a_pack("erin")]
        runtime.queue_waiting_since.update({"frank": 1.0, "erin": 2.0})
        self.pick_up("system_next_trigger_fallback")
        self.assertEqual(self.running(), ["frank"])
        self.assertEqual(self.woken(), [])

    def test_with_a_second_slot_free_the_nick_behind_it_starts_too(self):
        self.set_config(MAX_DCC_SLOTS=2)
        config.dcc_queue["erin"] = [a_pack("erin")]
        config.dcc_queue["frank"] = [queue_row(user="frank", filename="C.flac")]
        runtime.queue_waiting_since.update({"erin": 1.0, "frank": 2.0})
        self.pick_up("system_next_trigger_fallback")
        self.assertEqual(self.woken(), ["erin"])
        self.assertEqual(self.running(), ["frank"])

    def test_not_while_another_pack_is_being_made(self):
        """It could not start; the packer's release wakes it (#215)."""
        self.set_config(rar_inprogress=True)
        config.dcc_queue["erin"] = [a_pack("erin")]
        config.dcc_queue["frank"] = [queue_row(user="frank", filename="C.flac")]
        runtime.queue_waiting_since.update({"erin": 1.0, "frank": 2.0})
        self.pick_up("system_next_trigger_fallback")
        self.assertEqual(self.woken(), [])
        self.assertEqual(self.running(), ["frank"], "the slot is not left idle for it")

    def test_not_while_every_slot_is_busy(self):
        config.active_transfers.append({"user": "gina", "file": "X.flac", "bytes_sent": 0})
        config.dcc_queue["erin"] = [a_pack("erin")]
        self.pick_up("system_next_trigger_fallback")
        self.assertEqual(self.woken(), [])

    def test_an_absent_pack_owner_is_not_woken(self):
        config.channel_users[fair.OTHER].discard("erin")
        config.dcc_queue["erin"] = [a_pack("erin")]
        self.pick_up("system_next_trigger_fallback")
        self.assertEqual(self.woken(), [])


if __name__ == "__main__":
    unittest.main()
