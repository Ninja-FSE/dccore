"""A queued folder pack is picked up when the packer lock frees.

THE DEFECT (#215)

Packing runs strictly one at a time: `config.rar_inprogress` bounds it to one.
A second user asking for `!rar` while a pack is running is turned away at the
[RAR-HOLD] branch and their row stays queued, which is correct.

Nothing ever came back for them. `check_queue_and_send(irc_sock, completed_user)`
only ever inspects the queue of the user handed to it, and every caller hands it
the user whose transfer just finished - never the one turned away. So the held
row waited for that user to complete some unrelated transfer of their own, and
on a bot serving one album at a time that can be never.

The user is told their request is queued, and it is. Permanently.

THE FIX

Every path that releases `rar_inprogress` now looks for the next queued pack and
dispatches it. Per @Ninja-FSE's call on the issue: the queue popper rather than
the packer's own `finally:`, so scheduling stays in one place, and arrival order,
because a pack is the slowest item in the queue - prioritising it lets one album
stall a queue of single tracks, and deprioritising it lets a pack starve.
Arrival order is the only one of the three with no pathological case.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402


def pack_row(name="Album", user="dave"):
    return {"file": f"{name}.rar", "path": f"/music/{name}", "channel": "#chan",
            "user_raw": user, "is_unpacked_rar_folder": True,
            "is_temporary_zip": True}


def plain_row(name="Song.flac", user="dave"):
    return {"file": name, "path": f"/music/{name}", "channel": "#chan",
            "user_raw": user, "is_temporary_zip": False}


class FindingTheNextWaitingPack(DCCoreTestCase):
    """next_waiting_pack_owner() in isolation - the arrival-order decision."""

    def setUp(self):
        super().setUp()
        config.dcc_queue.clear()
        self.addCleanup(config.dcc_queue.clear)

    def test_nothing_queued_means_nobody_to_wake(self):
        self.assertIsNone(dcc.next_waiting_pack_owner())

    def test_a_queued_pack_is_found(self):
        config.dcc_queue["dave"] = [pack_row(user="Dave")]

        self.assertEqual(dcc.next_waiting_pack_owner(), "Dave")

    def test_the_original_capitalisation_is_returned(self):
        """check_queue_and_send() is handed this straight back, and the queue is
        keyed lowercase - handing back the key would address the user by a nick
        they did not use."""
        config.dcc_queue["dave"] = [pack_row(user="DaVe")]

        self.assertEqual(dcc.next_waiting_pack_owner(), "DaVe")

    def test_a_plain_file_is_not_a_pack(self):
        """Waking a user whose head is an ordinary transfer would dispatch a
        send under a lock released for packing."""
        config.dcc_queue["dave"] = [plain_row()]

        self.assertIsNone(dcc.next_waiting_pack_owner())

    def test_a_pack_behind_a_plain_file_is_not_dispatchable_yet(self):
        """check_queue_and_send() takes entries[0] and nothing else, so waking
        this user would be a no-op that looks like a fix."""
        config.dcc_queue["dave"] = [plain_row(), pack_row()]

        self.assertIsNone(dcc.next_waiting_pack_owner())

    def test_the_earliest_queued_user_wins(self):
        """Arrival order. dcc_queue is a dict and insertion order is what the
        rest of the queue already treats as arrival order."""
        config.dcc_queue["alice"] = [pack_row(user="alice")]
        config.dcc_queue["bob"] = [pack_row(user="bob")]

        self.assertEqual(dcc.next_waiting_pack_owner(), "alice")

    def test_the_user_who_just_finished_is_skipped(self):
        """Their own completion path already re-triggers them; waking them here
        as well would double-dispatch."""
        config.dcc_queue["alice"] = [pack_row(user="alice")]
        config.dcc_queue["bob"] = [pack_row(user="bob")]

        self.assertEqual(dcc.next_waiting_pack_owner(exclude_user="Alice"), "bob")

    def test_an_empty_queue_list_is_skipped_not_indexed(self):
        """A user row emptied by a !remove leaves []; entries[0] would raise."""
        config.dcc_queue["alice"] = []
        config.dcc_queue["bob"] = [pack_row(user="bob")]

        self.assertEqual(dcc.next_waiting_pack_owner(), "bob")


class ReleasingTheLockWakesTheNextPack(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.dcc_queue.clear()
        self.addCleanup(config.dcc_queue.clear)
        self.dispatched = []
        real = dcc.check_queue_and_send
        # Recorded rather than run: the real one packs an album.
        dcc.check_queue_and_send = lambda sock, who: self.dispatched.append(who)
        self.addCleanup(setattr, dcc, "check_queue_and_send", real)
        self.set_config(rar_inprogress=False)

    def wake(self, just_finished=None):
        return dcc.redispatch_waiting_pack(RecordingSocket(),
                                           just_finished=just_finished)

    def test_a_waiting_pack_is_dispatched(self):
        config.dcc_queue["bob"] = [pack_row(user="bob")]

        self.assertEqual(self.wake(just_finished="alice"), "bob")

    def test_nothing_waiting_dispatches_nothing(self):
        self.assertIsNone(self.wake(just_finished="alice"))

    def test_a_pack_still_running_is_not_disturbed(self):
        """The lock is cleared a line or two before this runs, but a concurrent
        trigger may already have claimed it - starting a second packer would
        defeat the interlock the whole branch exists to hold."""
        self.set_config(rar_inprogress=True)
        config.dcc_queue["bob"] = [pack_row(user="bob")]

        self.assertIsNone(self.wake(just_finished="alice"))

    def test_the_finisher_is_not_woken_again(self):
        config.dcc_queue["alice"] = [pack_row(user="alice")]

        self.assertIsNone(self.wake(just_finished="alice"))


class EveryReleasePathWakesTheQueue(unittest.TestCase):
    """The defect was not that one path forgot - it was that no path did it.

    Checked structurally: every `config.rar_inprogress = False` has to be
    followed by the wake, or a pack held while THAT particular path releases
    the lock is stranded exactly as before. Five sites, and a test that names
    the count so a sixth added later is not silently missed.
    """

    def release_sites(self):
        import ast
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            lines = handle.read().split("\n")
        return [n for n, line in enumerate(lines, 1)
                if line.strip() == "config.rar_inprogress = False"]

    def wake_lines(self):
        """Real Call nodes, not lines containing the name.

        The first version matched the text, so a commented-out call counted as
        a wake and a mutation run walked straight past it. Same defect #202
        replaced four other guards for: a scan of source text cannot tell a
        call from a mention.
        """
        import ast

        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        return {node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "redispatch_waiting_pack"}

    def test_every_release_is_followed_by_a_wake(self):
        wakes = self.wake_lines()
        unwoken = [n for n in self.release_sites()
                   if not any(n < w <= n + 8 for w in wakes)]

        self.assertEqual(
            unwoken, [],
            f"dcc.py releases rar_inprogress at line(s) {unwoken} without "
            f"dispatching a waiting pack - a pack held while that path runs "
            f"stays queued forever, which is #215 exactly")

    def test_the_scan_found_the_release_sites(self):
        """Control. Zero sites would pass the assertion above on any tree."""
        self.assertGreaterEqual(len(self.release_sites()), 3)


if __name__ == "__main__":
    unittest.main()
