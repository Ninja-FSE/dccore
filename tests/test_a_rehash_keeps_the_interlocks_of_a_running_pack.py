"""A rehash whose quiesce wait gave up under a running pack cleared the
packer's interlocks anyway (audit M49, #651).

wait_for_transfers_to_finish() counts a running folder pack as busy, but
after REHASH_TRANSFER_WAIT (120 s) it returns False and carries on - and
the caller never looked at the value. The reload then reset
config.rar_inprogress to False (defaults.py re-executes) and the rehash
rebound user_processing_lock to an empty set, both while `rar` was still
running (RAR_TIMEOUT is half an hour). The user's still-queued row passed
both interlocks on the next trigger for that nick - a second !rar, a JOIN
thaw, the freeze-abort timer - and a second packer started on the same
archive path, unlinking the file the first was writing. That is the
double-pack the packer's own docstring records fixing, reopened by a
dashboard Save two minutes into a nine-gigabyte box set.

The flags cannot say whether a pack is running: rar_inprogress is True both
while rar runs and after a packer died without releasing it, and the
documented "lock-clearing rehash" exists for the second case. The packer's
THREAD can. dcc.py now records it in runtime.packer_thread (runtime.py is
never reloaded), dcc.a_pack_is_running() reads it, and the rehash keeps
the interlocks while it is alive and clears them - the escape hatch, as
before - when it is not.
"""

import contextlib
import io
import os
import subprocess
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import platform_compat  # noqa: E402
import runtime  # noqa: E402

from tests import support  # noqa: E402
from tests.support import DCCoreTestCase, RecordingSocket, no_disk_writes, silence_debug  # noqa: E402

USER = "dave"


class TheDecision(unittest.TestCase):
    """clear_or_keep_pack_interlocks(), the pure half."""

    def cfg(self, rar=False, lock=()):
        holder = type("Cfg", (), {})()
        holder.rar_inprogress = rar
        holder.user_processing_lock = set(lock)
        return holder

    def test_a_running_pack_keeps_both(self):
        cfg = self.cfg(rar=False, lock=())     # what the reload just left
        logs = []

        cleared = commands.clear_or_keep_pack_interlocks(cfg, True, {USER}, log=logs.append)

        self.assertFalse(cleared)
        self.assertTrue(cfg.rar_inprogress, "the reload's reset was not undone")
        self.assertEqual(cfg.user_processing_lock, {USER})
        self.assertIn("still running for dave", logs[0])
        self.assertIn("Nothing was interrupted", logs[0])

    def test_no_pack_clears_both_as_the_escape_hatch_always_did(self):
        cfg = self.cfg(rar=True, lock={USER})

        cleared = commands.clear_or_keep_pack_interlocks(cfg, False, {USER}, log=lambda _m: None)

        self.assertTrue(cleared)
        self.assertFalse(cfg.rar_inprogress)
        self.assertEqual(cfg.user_processing_lock, set())

    def test_a_lock_the_reload_removed_is_recreated(self):
        cfg = self.cfg()
        del cfg.user_processing_lock

        commands.clear_or_keep_pack_interlocks(cfg, True, {USER}, log=lambda _m: None)

        self.assertEqual(cfg.user_processing_lock, {USER})


class TheThreadIsTheAnswer(DCCoreTestCase):
    """A real pack, driven to the point where `rar` is running - the
    subprocess call blocks on an Event - and released. The interlocks and
    runtime.packer_thread follow the thread, so the rehash can tell packing
    from wedged."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        CHANNEL="#somechannel", MAX_DCC_SLOTS=3, RAR_ENABLED=True,
                        TMP_ZIP_DIR=os.path.join(self.tree.root, "tmp"),
                        bot_joined_channel=True, rar_inprogress=False)
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        no_disk_writes(db)
        silence_debug(announce)
        config.channel_users["#somechannel"] = {USER}
        self.rar_started = threading.Event()
        self.let_rar_finish = threading.Event()

        def rar_that_waits(cmd, **_kwargs):
            self.rar_started.set()
            self.let_rar_finish.wait(20)
            return subprocess.CompletedProcess(cmd, 1, "", "stopped by the test")

        support.fake_rar_runs(self, dcc.subprocess, rar_that_waits)
        self._real_rar = platform_compat.rar_command
        platform_compat.rar_command = lambda configured=None: "rar-for-the-test"
        self.addCleanup(setattr, platform_compat, "rar_command", self._real_rar)
        # Cleanups run last-in-first-out (#828): the thread has to be JOINED
        # before the reference to it is dropped, or the join finds None, the
        # packer runs on into the next test, and its finally clears
        # rar_inprogress under that test's own pack. The reset is registered
        # first so it runs last.
        self.addCleanup(setattr, runtime, "packer_thread", None)
        self.addCleanup(self._let_everything_finish)

    def _let_everything_finish(self):
        self.let_rar_finish.set()
        thread = runtime.packer_thread
        if thread is not None:
            thread.join(10)
            self.assertFalse(thread.is_alive(), "the packer thread did not finish")

    def start_a_pack(self):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.handle_download_request(RecordingSocket(), USER,
                                        "!rar Metallica/Black Album (1991)", "#somechannel")
        self.assertTrue(self.rar_started.wait(10), "the packer never reached rar")

    def test_while_rar_runs_the_pack_is_running(self):
        self.assertFalse(dcc.a_pack_is_running(), "nothing has started yet")

        self.start_a_pack()

        self.assertTrue(dcc.a_pack_is_running())
        self.assertTrue(config.rar_inprogress)
        self.assertIn(USER, config.user_processing_lock)

    def test_and_the_rehashs_decision_keeps_the_interlocks(self):
        self.start_a_pack()
        # What the reload does to the scalar, then the decision the rehash makes.
        config.rar_inprogress = False

        cleared = commands.clear_or_keep_pack_interlocks(
            config, dcc.a_pack_is_running(), set(config.user_processing_lock), log=lambda _m: None)

        self.assertFalse(cleared)
        self.assertTrue(config.rar_inprogress)
        self.assertIn(USER, config.user_processing_lock)

    def test_once_rar_returns_the_pack_is_over_and_the_flags_are_released(self):
        self.start_a_pack()
        thread = runtime.packer_thread

        self.let_rar_finish.set()
        thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertFalse(dcc.a_pack_is_running())
        self.assertIsNone(runtime.packer_thread)
        self.assertFalse(config.rar_inprogress)
        self.assertNotIn(USER, config.user_processing_lock)

    def test_a_stale_flag_with_no_thread_is_the_wedged_case(self):
        """The escape hatch: flags set, nobody packing."""
        config.rar_inprogress = True
        config.user_processing_lock.add(USER)

        self.assertFalse(dcc.a_pack_is_running())
        cleared = commands.clear_or_keep_pack_interlocks(
            config, dcc.a_pack_is_running(), {USER}, log=lambda _m: None)

        self.assertTrue(cleared)
        self.assertFalse(config.rar_inprogress)


class TheRehashAsksBeforeAndAfterTheReload(TheThreadIsTheAnswer):
    """The real handle_rehash_request(), with only its module reload stood in
    for (tests.support.rehash_with_the_reload_stood_in), around a real pack
    whose rar is still running (#1469; this was read out of commands.py).
    The quiesce wait gives up at once - REHASH_TRANSFER_WAIT is 0 - which is
    the audit's case: the wait gave up under a running pack."""

    def setUp(self):
        super().setUp()
        self.set_config(REHASH_TRANSFER_WAIT=0)

    @staticmethod
    def what_the_reload_does():
        """defaults.py re-executes `rar_inprogress = False`."""
        config.rar_inprogress = False
        return []

    def test_a_pack_still_running_keeps_its_interlocks(self):
        self.start_a_pack()

        self.assertIs(support.rehash_with_the_reload_stood_in(self, lambda *_a, **_k: self.what_the_reload_does()),
                      True)

        self.assertTrue(dcc.a_pack_is_running(), "the control: rar is still running")
        self.assertTrue(config.rar_inprogress, "the rehash cleared the interlock of a running pack")
        self.assertIn(USER, config.user_processing_lock)

    def test_with_no_pack_the_stale_interlocks_are_cleared(self):
        """The escape hatch: flags set, nobody packing."""
        config.rar_inprogress = True
        config.user_processing_lock.add(USER)

        self.assertIs(support.rehash_with_the_reload_stood_in(self, lambda *_a, **_k: []), True)

        self.assertFalse(config.rar_inprogress, "a wedged packer's interlock survived the rehash")
        self.assertNotIn(USER, config.user_processing_lock)

    def test_a_pack_that_ends_during_the_reload_is_asked_about_again(self):
        """Asked before the reload, and again after it: a pack that finished
        meanwhile has released the interlocks itself, and keeping them on the
        first answer would latch them again with nobody left to release
        them."""
        self.start_a_pack()
        thread = runtime.packer_thread

        def reload_while_the_pack_ends(*_args, **_kwargs):
            self.let_rar_finish.set()
            thread.join(10)
            return self.what_the_reload_does()

        self.assertIs(support.rehash_with_the_reload_stood_in(self, reload_while_the_pack_ends), True)

        self.assertFalse(dcc.a_pack_is_running(), "the control: the pack has ended")
        self.assertFalse(config.rar_inprogress, "the interlock was latched again after the pack ended")
        self.assertNotIn(USER, config.user_processing_lock)


for _name in [n for n in dir(TheThreadIsTheAnswer) if n.startswith("test")]:
    setattr(TheRehashAsksBeforeAndAfterTheReload, _name, None)


if __name__ == "__main__":
    unittest.main()


class TheFixtureLeavesNoPackerBehind(unittest.TestCase):
    """The guard for #828: one of the tests above, run on its own, leaves
    no thread of its own alive. With the cleanups the wrong way round the
    packer thread was released but never joined, and ran its finally into
    the next test."""

    # The case run below is a DCCoreTestCase, so the settings its harness
    # sets are set inside this test too - on purpose, and not this test's
    # leak. Run first in a process (this module alone), they move from the
    # shipped values (#1469).
    SETTINGS_THE_HARNESS_SETS = DCCoreTestCase.SETTINGS_THE_HARNESS_SETS

    def test_a_pack_test_joins_its_packer_before_it_ends(self):
        before = set(threading.enumerate())
        case = TheThreadIsTheAnswer("test_while_rar_runs_the_pack_is_running")
        result = unittest.TestResult()

        case.run(result)

        self.assertEqual((result.failures, result.errors), ([], []))
        leaked = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        self.assertEqual(leaked, [], "the fixture's packer thread outlived the test")
        self.assertIsNone(runtime.packer_thread)
