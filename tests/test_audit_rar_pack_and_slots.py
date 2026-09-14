"""The folder packer's two blind spots, which are the same blind spot.

`config.active_transfers` is the SEND side. A folder pack has no row there for
as long as it runs: check_queue_and_send() claims `rar_inprogress`, runs `rar`
for up to RAR_TIMEOUT (half an hour by default), and only appends to
active_transfers once the archive exists.

Two things read active_transfers and drew the wrong conclusion from that.

THE QUIESCE SAW AN IDLE BOT

wait_for_transfers_to_finish() polls active_transfers, so it returned True
immediately while a pack was mid-flight. The reload then re-executed
defaults.py - whose body contains `rar_inprogress = False` - and commands.py
cleared `user_processing_lock` outright, both while the packer was still
running. The next `!rar` read both interlocks as free and started a SECOND rar
process. Two packs of the same album target the same archive path, and the
second one removes the file the first is still writing.

THE CAPACITY CHECK WAS MINUTES STALE

The RAR branch checks MAX_DCC_SLOTS before starting rar, and appends its slot
after. For a large album those are minutes apart - long enough for every slot
to fill with plain file sends, each of which re-checked correctly on its own
way through. This was the one append that did not.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class ARunningPackCountsAsBusy(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(active_transfers=[], rar_inprogress=False,
                        REHASH_TRANSFER_WAIT=60)
        self.addCleanup(lambda: setattr(config, "transfers_paused", False))

    def wait(self, sleep):
        return dcc.wait_for_transfers_to_finish(timeout=60, sleep=sleep,
                                                log=lambda _m: None)

    def test_the_wait_does_not_return_while_a_pack_runs(self):
        config.rar_inprogress = True
        ticks = []

        def tick(_seconds):
            ticks.append(1)
            if len(ticks) >= 3:
                config.rar_inprogress = False   # the pack finishes

        went_quiet = self.wait(tick)

        self.assertTrue(went_quiet)
        self.assertGreaterEqual(len(ticks), 3,
                                "the wait returned while a pack was running")

    def test_an_idle_bot_still_returns_at_once(self):
        """Control: the wait must not start blocking on a flag nobody set."""
        ticks = []

        went_quiet = self.wait(lambda _s: ticks.append(1))

        self.assertTrue(went_quiet)
        self.assertEqual(ticks, [])

    def test_a_pack_and_a_send_together_are_both_waited_for(self):
        config.rar_inprogress = True
        config.active_transfers.append({"user": "alice", "file": "a.flac"})
        ticks = []

        def tick(_seconds):
            ticks.append(1)
            if len(ticks) == 2:
                config.active_transfers.clear()
            if len(ticks) == 4:
                config.rar_inprogress = False

        self.assertTrue(self.wait(tick))
        self.assertGreaterEqual(len(ticks), 4)

    def test_the_pause_is_still_set_before_the_wait_begins(self):
        """Unchanged, and the thing the whole quiesce rests on: a send started
        during the wait is one the reload lands in the middle of."""
        config.rar_inprogress = True
        seen = []

        def tick(_seconds):
            seen.append(dcc.transfers_are_paused())
            config.rar_inprogress = False

        self.wait(tick)

        self.assertEqual(seen, [True])


class ThePackRechecksItsSlotBeforeTakingOne(DCCoreTestCase):
    """Asserted on the source, deliberately.

    Reaching the append means running `rar` for real against a real album -
    the branch is a subprocess call deep inside a 500-line function - and the
    defect is precisely whether a capacity test stands between the pack
    finishing and the slot being taken.
    """

    def source(self):
        import inspect

        return inspect.getsource(dcc.check_queue_and_send)

    def test_the_append_is_guarded_by_a_capacity_test(self):
        source = self.source()

        self.assertIn("room = len(config.active_transfers) < config.MAX_DCC_SLOTS",
                      source)
        self.assertIn("if room:", source)

    def test_it_is_taken_under_the_queue_lock(self):
        """Two dispatch threads reaching this together would otherwise both
        see room for the last slot."""
        source = self.source()
        after_check = source.split("the packed archive stays queued")[0]

        self.assertIn("with queue_lock:", after_check)

    def test_a_refused_pack_releases_the_interlock_it_holds(self):
        """Returning without clearing rar_inprogress would stop every future
        pack for every user until the daemon restarted - the exact failure the
        RAR_TIMEOUT comment above it warns about."""
        source = self.source()
        refusal = source.split("the packed archive stays queued")[1][:600]

        self.assertIn("config.rar_inprogress = False", refusal)
        self.assertIn("user_processing_lock", refusal)

    def test_a_refused_pack_lets_a_waiting_one_start(self):
        """redispatch_waiting_pack() is the only thing that revisits a user
        turned away at [RAR-HOLD]; without it they wait for a trigger that
        never comes."""
        source = self.source()
        refusal = source.split("the packed archive stays queued")[1][:600]

        self.assertIn("redispatch_waiting_pack", refusal)

    def test_the_pre_dispatch_checks_are_all_still_there(self):
        """Control on the control: this fix is about the ONE append that had
        no check, not about replacing the checks that were already right.

        Three of them - the RAR branch before it starts packing, the plain
        file path, and section B - all in the same form. The new one is a
        fourth, in a different form, because it has to hold the lock across
        the decision and the append."""
        source = self.source()

        self.assertEqual(
            source.count("if len(config.active_transfers) >= config.MAX_DCC_SLOTS:"),
            3)


if __name__ == "__main__":
    unittest.main()
