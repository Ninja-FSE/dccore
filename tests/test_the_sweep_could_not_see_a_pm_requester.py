"""#530: 65 files QUEUED for days on a live bot, 0 of 3 slots busy, while 38
files went to other people around them.

The user had requested by PRIVATE MESSAGE. A queue row records the wire
target of the request as its 'channel', and for a PM that is the bot's own
nick. check_queue_and_send()'s global sweep - section B, the path every
completed transfer and every !rehash takes - looked for the user in
channel_users[<that channel>], and no such key ever exists for a nick. So a
PM-originated head row was invisible to the sweep, whichever of our channels
the user was actually sitting in.

That alone would have been "PM requests are only served by their own
trigger". The second half made it permanent: where the specific-user branch
answers an absent user by FREEZING them and starting the five-minute
countdown, the sweep just `continue`d. No freeze, no timer, no expiry, no
log line. And the JOIN handler wakes only users who are frozen - so after a
restart (frozen_queues is in-memory), even a user who came and went was never
looked at again.

Three fixes, three classes below, and a fourth for the announce target that
the same PM row was quietly sending to the bot itself.
"""

import contextlib
import io
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import (  # noqa: E402
    DCCoreTestCase, RecordingSocket, no_disk_writes, queue_row, silence_debug)

SWEEP = "system_next_trigger_fallback"

# Invented for this file. The row's 'channel' is the bot's OWN nick for a PM
# request, so the fixture bot has a name of its own and the requester another.
BOT_NICK = "TestServeBot"
PM_USER = "pmrequester"
CHANNEL = "#dccore-test"


@contextlib.contextmanager
def quiet():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


class _FakeThread:
    """Records the target and never runs it: the freeze countdown sleeps in
    ten-second steps for five minutes, and a real dispatch opens a socket."""

    spawned = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **extra):
        self.target = target
        self.args = args
        _FakeThread.spawned.append((getattr(target, "__name__", str(target)), args))

    def start(self):
        return None

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


@contextlib.contextmanager
def no_threads():
    real = dcc.threading.Thread
    _FakeThread.spawned = []
    dcc.threading.Thread = _FakeThread
    try:
        yield _FakeThread.spawned
    finally:
        dcc.threading.Thread = real


def pm_row(user=PM_USER, filename="Song.flac", **extra):
    """The exact shape on the reporting bot's disk: channel = the bot's nick."""
    return queue_row(user=user, filename=filename, channel=BOT_NICK, **extra)


class _SweepCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self._real_send_debug = announce.send_debug
        self._real_save_queue = db.save_dcc_queue
        self._real_save_bans = db.save_bans_to_file
        self._real_save_stats = db.save_advanced_stats
        self.debug = silence_debug(announce)
        no_disk_writes(db)
        self.sock = RecordingSocket()
        self.set_config(NICKNAME=BOT_NICK, CHANNEL=CHANNEL, MAX_DCC_SLOTS=3)
        config.bot_joined_channel = True
        config.channel_users = {CHANNEL: {"someone_else"}}
        config.frozen_queues.clear()
        config.active_transfers.clear()
        config.user_processing_lock.clear()

    def tearDown(self):
        announce.send_debug = self._real_send_debug
        db.save_dcc_queue = self._real_save_queue
        db.save_bans_to_file = self._real_save_bans
        db.save_advanced_stats = self._real_save_stats
        super().tearDown()

    def sweep(self):
        with no_threads() as spawned, quiet():
            dcc.check_queue_and_send(self.sock, SWEEP)
        return spawned

    @staticmethod
    def dispatched(spawned):
        return [args[1] for name, args in spawned if name == "start_dcc_send"]

    @staticmethod
    def timers(spawned):
        return [args[1] for name, args in spawned if name == "user_queue_timer"]


class TheSweepSeesAPmRequesterWhoIsHere(_SweepCase):
    """Defect 1. The user is in our channel; the row says the bot's nick."""

    def test_a_pm_row_is_dispatched_by_the_sweep(self):
        config.dcc_queue[PM_USER] = [pm_row()]
        config.channel_users[CHANNEL].add(PM_USER)

        spawned = self.sweep()

        self.assertEqual(self.dispatched(spawned), [PM_USER],
                         "the sweep could not see a user whose row names the "
                         "bot's nick as its channel")
        self.assertEqual([tx["user"] for tx in config.active_transfers], [PM_USER])
        self.assertNotIn(PM_USER, config.frozen_queues)

    def test_presence_in_any_of_our_channels_counts(self):
        """Two channels; the user is in the second. The row named neither."""
        self.set_config(CHANNEL="#dccore-test,#dccore-other")
        config.channel_users = {"#dccore-test": {"someone_else"},
                                "#dccore-other": {PM_USER}}
        config.dcc_queue[PM_USER] = [pm_row()]

        spawned = self.sweep()

        self.assertEqual(self.dispatched(spawned), [PM_USER])

    def test_the_announce_target_is_a_channel_not_the_bot(self):
        """The same PM row handed the bot's nick to start_dcc_send() as the
        place to announce completion. A "Sent:" line to ourselves."""
        config.dcc_queue[PM_USER] = [pm_row()]
        config.channel_users[CHANNEL].add(PM_USER)

        spawned = self.sweep()

        (args,) = [args for name, args in spawned if name == "start_dcc_send"]
        self.assertEqual(args[4], CHANNEL,
                         "completion would be announced to %r" % (args[4],))

    def test_a_row_that_names_a_real_channel_still_announces_there(self):
        """The fix must not flatten every row onto the default channel."""
        self.set_config(CHANNEL="#dccore-test,#dccore-other")
        config.channel_users = {"#dccore-test": set(), "#dccore-other": {PM_USER}}
        config.dcc_queue[PM_USER] = [queue_row(user=PM_USER, channel="#dccore-other")]

        spawned = self.sweep()

        (args,) = [args for name, args in spawned if name == "start_dcc_send"]
        self.assertEqual(args[4], "#dccore-other")


class TheSweepFreezesAnAbsentUser(_SweepCase):
    """Defect 2. Silence was the whole problem: an absent user the sweep
    passed over was neither served nor let go."""

    def setUp(self):
        super().setUp()
        self.rows = [pm_row(), pm_row(filename="Other.flac")]
        config.dcc_queue[PM_USER] = self.rows

    def test_an_absent_user_is_frozen_and_the_countdown_started(self):
        spawned = self.sweep()

        self.assertIn(PM_USER, config.frozen_queues,
                      "the sweep skipped an absent user without freezing them")
        self.assertAlmostEqual(config.frozen_queues[PM_USER], time.time(), delta=5.0)
        self.assertEqual(self.timers(spawned), [PM_USER])
        self.assertEqual(self.dispatched(spawned), [])
        self.assertTrue(any(cat == "QUIT" for cat, _ in self.debug),
                        "the freeze is announced to the debug channel on the "
                        "specific-user path; the sweep must not be quieter")

    def test_the_queue_itself_is_kept_for_the_countdown(self):
        self.sweep()

        self.assertIs(config.dcc_queue[PM_USER], self.rows)
        self.assertEqual([r["file"] for r in self.rows], ["Song.flac", "Other.flac"])

    def test_a_second_sweep_does_not_restart_the_countdown(self):
        self.sweep()
        stamp = config.frozen_queues[PM_USER] - 120.0
        config.frozen_queues[PM_USER] = stamp

        spawned = self.sweep()

        self.assertEqual(config.frozen_queues[PM_USER], stamp)
        self.assertEqual(self.timers(spawned), [])

    def test_every_absent_user_in_the_pass_is_frozen(self):
        """The scan visits every waiting user looking for one to serve; each
        absent one it passes gets the same treatment, not just the first."""
        config.dcc_queue["otherabsent"] = [queue_row(user="otherabsent")]

        spawned = self.sweep()

        self.assertEqual(sorted(self.timers(spawned)), sorted([PM_USER, "otherabsent"]))

    def test_and_five_minutes_later_the_queue_is_gone(self):
        """The two halves connect: the freeze the sweep now applies is the
        same one the stale-freeze sweep at the top of the function reaps."""
        self.sweep()
        config.frozen_queues[PM_USER] = time.time() - 301.0

        self.sweep()

        self.assertNotIn(PM_USER, config.dcc_queue)
        self.assertNotIn(PM_USER, config.frozen_queues)

    def test_nothing_is_frozen_while_the_bot_is_not_synced(self):
        """Same guard as the specific-user branch: an empty channel_users
        after a reconnect is not evidence that anybody left."""
        config.bot_joined_channel = False
        config.channel_users = {}

        spawned = self.sweep()

        self.assertNotIn(PM_USER, config.frozen_queues)
        self.assertEqual(self.timers(spawned), [])
        self.assertIs(config.dcc_queue[PM_USER], self.rows)

    def test_the_freeze_is_announced_outside_the_queue_lock(self):
        """send_debug paces itself against the outbound flood limit. Holding
        queue_lock across it would stall every request handler for the length
        of that wait. threading.Lock is not reentrant, so a non-blocking
        acquire from inside the fake tells whether this thread holds it."""
        held = []

        def probing_send_debug(msg_text, category="INFO", notice=None):
            got = dcc.queue_lock.acquire(blocking=False)
            held.append(not got)
            if got:
                dcc.queue_lock.release()

        announce.send_debug = probing_send_debug

        self.sweep()

        self.assertTrue(held, "the freeze never reached send_debug")
        self.assertEqual(held, [False] * len(held),
                         "the sweep announced a freeze while holding queue_lock")

    def test_a_user_with_all_slots_busy_is_not_judged(self):
        """The sweep does not run at all when there is nowhere to put a
        transfer; that has not changed, and it must not freeze in passing."""
        for i in range(3):
            config.active_transfers.append({"user": "busy%d" % i, "file": "x", "bytes_sent": 0})

        spawned = self.sweep()

        self.assertEqual(self.timers(spawned), [])
        self.assertNotIn(PM_USER, config.frozen_queues)


class ARestoredQueueIsLookedAtOnActivation(_SweepCase):
    """Fix 3. A queue read back from dcc_queue.txt has no trigger of its
    own; a quiet bot never looked at it."""

    def test_present_users_are_served_up_to_the_slot_count(self):
        """One pass dispatches one user and breaks, so a single sweep would
        start one transfer and leave two idle slots and two present users."""
        for i in range(3):
            user = "restored%d" % i
            config.dcc_queue[user] = [queue_row(user=user)]
            config.channel_users[CHANNEL].add(user)

        with no_threads() as spawned, quiet():
            dcc.wake_restored_queues(self.sock)

        self.assertEqual(sorted(self.dispatched(spawned)),
                         ["restored0", "restored1", "restored2"])

    def test_absent_users_are_frozen_by_it(self):
        config.dcc_queue[PM_USER] = [pm_row()]

        with no_threads() as spawned, quiet():
            dcc.wake_restored_queues(self.sock)

        self.assertEqual(self.timers(spawned), [PM_USER])

    def test_it_is_a_no_op_with_nothing_queued(self):
        with no_threads() as spawned, quiet():
            dcc.wake_restored_queues(self.sock)

        self.assertEqual(spawned, [])

    def test_a_pause_still_wins(self):
        """The quiesce gate at the top of check_queue_and_send() is what a
        rehash relies on; activation must not dispatch through it."""
        config.dcc_queue[PM_USER] = [pm_row()]
        config.channel_users[CHANNEL].add(PM_USER)
        config.transfers_paused = True
        self.addCleanup(setattr, config, "transfers_paused", False)

        with no_threads() as spawned, quiet():
            dcc.wake_restored_queues(self.sock)

        self.assertEqual(spawned, [])

    def test_activation_runs_it_once_channel_users_is_trusted(self):
        """Read from the source: delayed_activate is a closure inside
        irc_loop. The call has to sit in the branch that just claimed
        channel sync, after the claim - the same reason that branch exists:
        with channel_users empty every waiting user looks absent."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def delayed_activate("):]
        body = body[:body.index("def background_nick_monitor(")]

        claimed = body.index("config.bot_joined_channel = True")
        woken = body.index("dcc.wake_restored_queues")
        unsynced = body.index("No channel members known yet")

        self.assertLess(claimed, woken, "the sweep runs before channel sync is claimed")
        self.assertLess(woken, unsynced, "the sweep is not inside the synced branch")


class ANickIsNotAPlaceToAnnounce(DCCoreTestCase):
    """announce_channel_for() handed back whatever string the row carried. For
    a PM row that is the bot's nick, and the "Sent:" line went to ourselves."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#one,#two", NICKNAME=BOT_NICK)

    def test_the_bots_own_nick_falls_back_to_the_default_channel(self):
        self.assertEqual(dcc.announce_channel_for({"channel": BOT_NICK}), "#one")

    def test_any_nick_does(self):
        """Not only ours: a row cannot legitimately name a nick at all."""
        self.assertEqual(dcc.announce_channel_for({"channel": "somebodyelse"}), "#one")

    def test_a_channel_is_still_kept(self):
        for chan in ("#chosen", "&local", "+modeless", "!safe"):
            with self.subTest(chan=chan):
                self.assertEqual(dcc.announce_channel_for({"channel": chan}), chan)

    def test_is_channel_name(self):
        for yes in ("#a", "&a", "+a", "!a", "  #padded "):
            self.assertTrue(dcc.is_channel_name(yes), yes)
        for no in ("nick", "", None, "   ", 12, ["#a"]):
            self.assertFalse(dcc.is_channel_name(no), repr(no))


class TheSpecificUserPathStillFreezes(_SweepCase):
    """The freeze moved into freeze_absent_user() so both paths share it.
    tests/test_reconnect.py covers the specific-user branch in depth; this
    pins the one thing the refactor could have lost: the branch still calls
    the shared policy rather than a second copy of it."""

    def test_the_specific_user_branch_uses_the_shared_helper(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def check_queue_and_send("):]
        body = body[:body.index("# B) Global queue handling")]

        self.assertIn("freeze_absent_user(irc_sock, completed_user, target_chan)", body)
        self.assertNotIn("def user_queue_timer", body,
                         "a second copy of the countdown is back inside the branch")

    def test_a_direct_trigger_for_an_absent_user_still_freezes(self):
        config.dcc_queue[PM_USER] = [pm_row()]

        with no_threads() as spawned, quiet():
            dcc.check_queue_and_send(self.sock, PM_USER)

        self.assertIn(PM_USER, config.frozen_queues)
        self.assertEqual(self.timers(spawned), [PM_USER])


if __name__ == "__main__":
    unittest.main()
