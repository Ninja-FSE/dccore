"""The frozen-queue sweep reads the channel lists once, not once per frozen nick.

#1140, from the performance audit. Step 1 of check_queue_and_send() visits
every frozen queue and asks user_is_present_in_ram() whether its nick is back.
That function walks every nick in every channel, lowercasing each, and the
sweep asked it once per frozen nick - under queue_lock, on every finished
transfer. After a netsplit, with a hundred queues frozen at once, that was a
hundred full scans (about 180 ms) while requests and the dispatcher waited on
the lock.

The sweep now takes one lowercased set of the nicks it can see and looks each
frozen nick up in it - the same comparison, both sides lowercased, so a nick
stored in mixed case still matches. With a single frozen nick it keeps the
early-exit scan, which is cheaper than building the set.
"""

import random
import time
import unittest
from unittest import mock

from tests.support import (DCCoreTestCase, queue_row, silence_debug,
                           no_disk_writes, CapturedDispatch, RecordingSocket)
from tests.test_reconnect import no_threads, quiet

import announce
import db
import dcc
import defaults as config

FREEZE_TIMEOUT = dcc.FREEZE_TIMEOUT
CHANNEL = "#dccore-test"          # the channel queue_row() files rows under


class TheFrozenSweep(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        real_send_debug = announce.send_debug
        real_saves = (db.save_dcc_queue, db.save_bans_to_file, db.save_advanced_stats)
        silence_debug(announce)
        no_disk_writes(db)
        self.addCleanup(setattr, announce, "send_debug", real_send_debug)
        self.addCleanup(lambda: (setattr(db, "save_dcc_queue", real_saves[0]),
                                 setattr(db, "save_bans_to_file", real_saves[1]),
                                 setattr(db, "save_advanced_stats", real_saves[2])))
        self.sock = RecordingSocket()
        config.bot_joined_channel = True

    def freeze(self, nick, age):
        config.dcc_queue[nick] = [queue_row(user=nick, filename=nick + ".flac")]
        config.frozen_queues[nick] = time.time() - age

    def sweep(self):
        with CapturedDispatch(dcc), no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "system_next_trigger_fallback")

    # --- how often the channel lists are read ------------------------------

    def test_many_frozen_nicks_read_the_lists_once(self):
        config.channel_users = {CHANNEL: {"bystander%d" % i for i in range(50)}}
        for i in range(20):
            self.freeze("away%d" % i, age=10)

        with mock.patch.object(dcc, "nicks_in_our_channels",
                               wraps=dcc.nicks_in_our_channels) as whole_set, \
                mock.patch.object(dcc, "user_is_present_in_ram",
                                  wraps=dcc.user_is_present_in_ram) as per_nick:
            self.sweep()

        self.assertEqual(whole_set.call_count, 1)
        asked = [c.args[0] for c in per_nick.call_args_list]
        self.assertEqual([n for n in asked if n.startswith("away")], [],
                         "the sweep still scans the channel lists once per frozen nick")
        self.assertEqual(len(config.frozen_queues), 20, "fresh freezes must survive")

    def test_two_frozen_nicks_are_already_many(self):
        config.channel_users = {CHANNEL: {"bystander"}}
        self.freeze("away1", age=10)
        self.freeze("away2", age=10)

        with mock.patch.object(dcc, "nicks_in_our_channels",
                               wraps=dcc.nicks_in_our_channels) as whole_set, \
                mock.patch.object(dcc, "user_is_present_in_ram",
                                  wraps=dcc.user_is_present_in_ram) as per_nick:
            self.sweep()

        self.assertEqual(whole_set.call_count, 1)
        self.assertEqual(per_nick.call_count, 0)

    def test_one_frozen_nick_keeps_the_early_exit_scan(self):
        """Building the whole set costs about twice one scan that can stop at
        the first match, so a lone frozen nick is asked about directly."""
        config.channel_users = {CHANNEL: {"bystander"}}
        self.freeze("away1", age=10)

        with mock.patch.object(dcc, "nicks_in_our_channels",
                               wraps=dcc.nicks_in_our_channels) as whole_set, \
                mock.patch.object(dcc, "user_is_present_in_ram",
                                  wraps=dcc.user_is_present_in_ram) as per_nick:
            self.sweep()

        self.assertEqual(whole_set.call_count, 0)
        self.assertEqual([c.args[0] for c in per_nick.call_args_list], ["away1"])

    # --- the answers are the ones the per-nick scan gave -------------------

    def test_case_does_not_matter_on_either_side(self):
        """Nicks come back from NAMES in any case, and a frozen key written by
        hand or by an older version may not be lowercase either."""
        config.channel_users = {CHANNEL: {"SomeUser", "otheruser", "bystander"}}
        self.freeze("someuser", age=10)                  # lowercase key, mixed-case nick
        self.freeze("OtherUser", age=10)                 # mixed-case key, lowercase nick
        self.freeze("away1", age=10)

        self.sweep()

        self.assertEqual(sorted(config.frozen_queues), ["away1"])

    def test_the_outcome_matches_the_per_nick_scan(self):
        """Random channel lists and freezes, in random case, against what
        user_is_present_in_ram() says of each nick before the sweep runs:
        present nicks thaw, absent ones past the timeout are dropped, the
        rest stay frozen."""
        rng = random.Random(1140)
        for trial in range(40):
            with self.subTest(trial=trial):
                config.dcc_queue.clear()
                config.frozen_queues.clear()
                pool = ["user%d" % i for i in range(30)]
                in_channel = set()
                for nick in rng.sample(pool, rng.randint(0, 20)):
                    in_channel.add(rng.choice([nick, nick.upper(), nick.capitalize()]))
                config.channel_users = {CHANNEL: in_channel,
                                        "#somechannel": {"bystander"}}
                for nick in rng.sample(pool, rng.randint(2, 15)):
                    key = rng.choice([nick, nick.upper()])
                    self.freeze(key, age=rng.choice([10, FREEZE_TIMEOUT + 60]))

                before = dict(config.frozen_queues)
                back = {k for k in before if dcc.user_is_present_in_ram(k)}
                expired = {k for k in before if k not in back
                           and time.time() - before[k] > FREEZE_TIMEOUT}

                self.sweep()

                # Judged on the keys the sweep was given. Section B, which runs
                # after it, freezes an absent queue under its lowercased key, so
                # a mixed-case key that stays frozen can gain a lowercase twin;
                # that is section B's business, not the sweep's.
                still_frozen = {k for k in before if k in config.frozen_queues}
                self.assertEqual(still_frozen, set(before) - back - expired)
                for key in expired:
                    self.assertNotIn(key, config.dcc_queue)
                for key in set(before) - back - expired:
                    self.assertIn(key, config.dcc_queue)


if __name__ == "__main__":
    unittest.main()
