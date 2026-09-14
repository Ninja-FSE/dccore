"""A rehash must never show a running daemon a configuration it does not have.

WHAT WENT WRONG

Two defects, one report, both in the rehash path.

1. THE RELOAD WINDOW. `importlib.reload(defaults)` re-executes defaults.py from
   the top. That file is a long list of literal assignments - NICKNAME = None,
   CHANNEL = None, ADMIN_NICK = None - and `settings_file.apply_to(globals())`
   runs only at the very end of it. So for the whole of a reload, every setting
   the operator configured is transiently back to its shipped default.

   Measured against a real install's settings.conf: a thread looping on
   config.NICKNAME during the reload saw it blank for 52% of the window.

   An operator saved DEBUG_CHANNEL from the dashboard. The browser re-fetched
   /api/settings the moment the response said "Rehash started", landed inside
   the window, and the Settings page came back with Nickname, Admin nick(s) and
   Channels EMPTY - on a daemon whose settings.conf had all three, intact, the
   entire time. Only those three looked wrong because every other setting on
   that page has a shipped default that happens to match what most operators
   run, so it renders identically either way.

2. THE DEBUG CHANNEL WAS NEVER JOINED. The rehash's channel sync built its
   list from CHANNEL alone; DEBUG_CHANNEL appeared in that block only in the
   test that stops it being PARTed. irc.py joins it at connect and nothing
   joins it after that. So setting a debug channel on a running bot reported
   success and did nothing until the next restart.

Both share the signature that keeps costing this project real installs:
NOTHING FAILED. No exception, no error line, no failing test - just a page
telling the truth about a state that should never have been observable, and a
JOIN that was never sent.
"""

import io
import contextlib
import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import commands  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import settings_file  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheDebugChannelIsSomewhereTheBotShouldBe(DCCoreTestCase):
    """_channels_to_sync() is both sides of the JOIN/PART comparison. The bug
    was that the debug channel was in neither."""

    def test_the_debug_channel_is_included(self):
        self.set_config(CHANNEL="#one,#two", DEBUG_CHANNEL="#bot-debug")

        self.assertIn("#bot-debug", commands._channels_to_sync(self.config))

    def test_the_reported_case(self):
        """The shape of the install that found it, with invented names: six
        channels configured, a debug channel added afterwards from the
        dashboard.

        The names were that operator's real ones until the pre-publication
        sweep - see tests/test_channel_list.py, which carried the same six."""
        self.set_config(
            CHANNEL="#Alpha,#bravo,#charlie,#delta-two,"
                    "#echo,#foxtrot",
            DEBUG_CHANNEL="#bot-debug")

        chans = commands._channels_to_sync(self.config)

        self.assertEqual(len(chans), 7)
        self.assertIn("#bot-debug", chans)

    def test_a_blank_debug_channel_adds_nothing(self):
        """Blank is the shipped default and means "no debug channel", not a
        channel named "". Joining "" would be a malformed JOIN."""
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                self.set_config(CHANNEL="#one", DEBUG_CHANNEL=blank)

                self.assertEqual(commands._channels_to_sync(self.config), ["#one"])

    def test_it_is_not_added_twice_when_it_is_already_a_main_channel(self):
        """An operator may well point DEBUG_CHANNEL at a channel already in
        CHANNEL. A duplicate would be JOINed and NAMEd twice."""
        self.set_config(CHANNEL="#one,#two", DEBUG_CHANNEL="#TWO")

        self.assertEqual(commands._channels_to_sync(self.config), ["#one", "#two"])

    def test_an_unchanged_debug_channel_produces_no_join(self):
        """THE REASON IT IS ON BOTH SIDES. old_chans and new_chans both come
        from this helper, so a debug channel that has not changed appears in
        both and the sync has nothing to do. Putting it only in new_chans would
        have fixed the reported bug and re-JOINed the channel, with a "due to
        new configuration layout!" debug line, on every rehash for ever."""
        self.set_config(CHANNEL="#one", DEBUG_CHANNEL="#bot-debug")
        old_chans = commands._channels_to_sync(self.config)

        new_chans = commands._channels_to_sync(self.config)

        self.assertEqual([c for c in new_chans if c not in old_chans], [])

    def test_a_newly_set_debug_channel_produces_exactly_one_join(self):
        """The reported sequence: no debug channel, then one."""
        self.set_config(CHANNEL="#one", DEBUG_CHANNEL="")
        old_chans = commands._channels_to_sync(self.config)

        self.set_config(CHANNEL="#one", DEBUG_CHANNEL="#bot-debug")
        new_chans = commands._channels_to_sync(self.config)

        self.assertEqual([c for c in new_chans if c not in old_chans], ["#bot-debug"])

    def test_whitespace_and_case_are_normalised(self):
        """The comparison is by name, and IRC channel names are
        case-insensitive. " #Bot-Debug " and "#bot-debug" are one channel."""
        self.set_config(CHANNEL=" #One , #two ", DEBUG_CHANNEL="  #Bot-Debug  ")

        self.assertEqual(commands._channels_to_sync(self.config),
                         ["#one", "#two", "#bot-debug"])

    def test_a_blank_channel_setting_does_not_raise(self):
        """CHANNEL is in settings_file.REQUIRED, but this runs on a config
        object anything can assign to - and it used to be a bare
        config.CHANNEL.split(","), which is an AttributeError on None. That
        exception would land on the rehash thread, after the reload and before
        the channel sync finished."""
        for blank in ("", None):
            with self.subTest(blank=blank):
                self.set_config(CHANNEL=blank, DEBUG_CHANNEL="#bot-debug")

                self.assertEqual(commands._channels_to_sync(self.config),
                                 ["#bot-debug"])


class TheReloadWindowIsNotObservable(unittest.TestCase):
    """The property: no thread holding the lock ever sees a REQUIRED setting
    blank, however long the reload takes or however often it runs."""

    def test_a_reader_holding_the_lock_never_sees_a_blank_required_setting(self):
        import defaults as config

        blanks = []
        reads = [0]
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                with runtime.config_reload_lock:
                    for name in settings_file.REQUIRED:
                        if not getattr(config, name, None):
                            blanks.append(name)
                reads[0] += 1

        # Only meaningful if the settings actually resolve to something in this
        # environment - otherwise "never blank" is vacuously true.
        if any(not getattr(config, name, None) for name in settings_file.REQUIRED):
            self.skipTest("no REQUIRED settings are configured in this checkout")

        worker = threading.Thread(target=reader, daemon=True)
        worker.start()
        try:
            for _ in range(8):
                commands.reload_modules_in_order(modules=('defaults',),
                                                 reload_self=False)
        finally:
            stop.set()
            worker.join(5)

        self.assertGreater(reads[0], 0, "the reader never ran")
        self.assertEqual(blanks, [],
                         f"a lock-holding reader saw {sorted(set(blanks))} blank "
                         f"during a reload - the window is observable again")

    def test_the_reload_actually_holds_the_lock(self):
        """The guard above passes trivially if the reload never takes the lock -
        a reader would then simply never contend. Assert the reload really is
        holding it, by watching from another thread."""
        held = []
        ready = threading.Event()
        done = threading.Event()

        def watcher():
            ready.set()
            # acquire(False) fails only while another thread holds it.
            while not done.is_set():
                if not runtime.config_reload_lock.acquire(blocking=False):
                    held.append(True)
                    return
                runtime.config_reload_lock.release()

        worker = threading.Thread(target=watcher, daemon=True)
        worker.start()
        ready.wait(2)
        try:
            for _ in range(30):
                commands.reload_modules_in_order(modules=('defaults',),
                                                 reload_self=False)
                if held:
                    break
        finally:
            done.set()
            worker.join(5)

        self.assertTrue(held, "no other thread was ever blocked by the reload - "
                              "reload_modules_in_order() is not holding "
                              "runtime.config_reload_lock")


class ARequiredSettingThatComesBackBlankIsKept(DCCoreTestCase):
    """The backstop, for when the window does not close cleanly.

    settings_file.apply_to() is deliberately forgiving about an unreadable
    settings.conf - right at startup, where oserve.startup()'s REQUIRED gate
    then refuses to boot and says why; wrong at rehash, where there is no gate
    and the daemon simply carries on with nothing configured.
    """

    def test_a_value_that_survives_is_left_alone(self):
        """Control: the ordinary rehash must not be touched by the backstop."""
        import defaults as config

        before = {name: getattr(config, name, None)
                  for name in settings_file.REQUIRED}

        commands.reload_modules_in_order(modules=('defaults',), reload_self=False)

        for name, value in before.items():
            self.assertEqual(getattr(config, name, None), value)

    def test_a_blank_after_reload_is_restored_from_the_running_value(self):
        """The failure this exists for: settings.conf readable a moment ago and
        not now - antivirus holding it open, a network share blinking, an
        editor mid-save. apply_to() logs that and keeps the built-in defaults,
        which at this point in the daemon's life means no nickname, no channels
        and no admin on a bot that is already connected and serving.

        Driven through the real function with importlib.reload replaced by one
        that blanks the setting, because that blanking IS the observable effect
        of the read failure - reproducing the antivirus is not the point.
        """
        import importlib

        import defaults as config

        self.set_config(NICKNAME="StillRunningAs")

        def blanking_reload(module):
            config.NICKNAME = ""
            return module

        real_reload = importlib.reload
        importlib.reload = blanking_reload
        try:
            commands.reload_modules_in_order(modules=('runtime',),
                                             reload_self=False)
        finally:
            importlib.reload = real_reload

        self.assertEqual(config.NICKNAME, "StillRunningAs",
                         "a REQUIRED setting that came back blank was not "
                         "restored - a rehash can de-configure a running bot")

    def test_only_required_settings_are_backstopped(self):
        """The narrowness is the safety. If this covered every setting it would
        be a ratchet: no rehash could ever unset anything, because the previous
        value would always be put back. DEBUG_CHANNEL - the very setting whose
        save started all this - must NOT be protected, or clearing it on the
        dashboard would silently do nothing."""
        import importlib

        import defaults as config

        self.assertNotIn("DEBUG_CHANNEL", settings_file.REQUIRED)
        self.set_config(DEBUG_CHANNEL="#bot-debug")

        def blanking_reload(module):
            config.DEBUG_CHANNEL = ""
            return module

        real_reload = importlib.reload
        importlib.reload = blanking_reload
        try:
            commands.reload_modules_in_order(modules=('runtime',),
                                             reload_self=False)
        finally:
            importlib.reload = real_reload

        self.assertEqual(config.DEBUG_CHANNEL, "",
                         "a non-REQUIRED setting was restored by the backstop - "
                         "no rehash could then ever unset one")

    def test_a_value_that_was_already_blank_is_not_invented(self):
        """A fresh install has no ADMIN_NICK yet and is about to be told so by
        the REQUIRED gate. The backstop must not paper over that."""
        import importlib

        import defaults as config

        self.set_config(NICKNAME="")

        real_reload = importlib.reload
        importlib.reload = lambda module: module
        try:
            commands.reload_modules_in_order(modules=('runtime',),
                                             reload_self=False)
        finally:
            importlib.reload = real_reload

        self.assertEqual(config.NICKNAME, "")


class TheDashboardReadsUnderTheLock(unittest.TestCase):
    """Read out of the source rather than by timing: the race is real but
    winning it on demand in a test is not reliable, and a payload builder that
    stopped taking the lock would pass every timing-based check on a fast
    machine."""

    def test_build_settings_payload_takes_the_reload_lock(self):
        with io.open(os.path.join(REPO_ROOT, "webserver.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        body = source.split("def build_settings_payload(", 1)[1]
        body = body.split("\ndef ", 1)[0]
        # The docstring names the lock too, and an assertion that matches prose
        # passes on a function that has stopped taking it - which is exactly
        # what the mutation run found. Look for the statement, not the noun.
        body = body.split('"""', 2)[-1]

        self.assertIn("with runtime.config_reload_lock:", body,
                      "build_settings_payload() no longer reads under "
                      "runtime.config_reload_lock - the Settings page can show "
                      "a half-reloaded configuration again")


class ARehashDoesNotSpendTheQueuesRetries(DCCoreTestCase):
    """Reported from a live install: "if the bot had some queues from a user,
    and admin made a rehash,
    it cancels the queue."

    Traced to the wake. Every rehash ends by calling check_queue_and_send() to
    let queued users into the free slots - and if that attempt fails, the
    failure is charged to the user's retry budget. MAX_SEND_FAILS is 3, so
    three rehashes deleted the row.

    The cause of the failure is what makes it wrong. "No usable public
    address" is the BOT's configuration: it affects every queued user
    identically and is fixed by the operator setting MY_IP_OR_DOCK, not by the
    user waiting. A file that is missing or empty is a different thing - that
    row really is dead, and charging it is what stops it being re-selected
    every three seconds for ever.
    """

    def setUp(self):
        super().setUp()
        self.set_config(dcc_queue={}, frozen_queues={}, channel_users={},
                        MY_IP_OR_DOCK="", MAX_SEND_FAILS=3)
        config.dcc_queue["someuser"] = [{"file": "T.flac", "path": "/m/T.flac"}]
        # #430: start_dcc_send() dispatches through the LIVE socket
        # sys.modules['oserve'] reports rather than trusting the `None`
        # passed below - which stays None deliberately, since these tests
        # are about the address/missing-file branches that run before any
        # send is attempted, not about the connection itself.
        self.oserve.irc_connection = object()

    def row(self):
        return config.dcc_queue.get("someuser", [None])[0]

    def attempt(self):
        """One dispatch attempt for the queued user, as the wake makes."""
        row = config.dcc_queue["someuser"][0]
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.start_dcc_send(None, "someuser", row["path"], row["file"],
                               "#chan", row)

    def test_no_public_address_does_not_charge_the_user(self):
        for _ in range(5):
            self.attempt()

        self.assertIn("someuser", config.dcc_queue)
        self.assertNotIn("send_fails", self.row() or {},
                         "an unset MY_IP_OR_DOCK is being charged to the user")

    def test_the_queue_survives_repeated_rehash_wakes(self):
        """The shape of the report: it took three, and the third one was
        silent about what it had just thrown away."""
        for _ in range(4):
            self.attempt()

        self.assertEqual(len(config.dcc_queue.get("someuser", [])), 1)

    def test_the_log_says_the_address_is_what_is_missing(self):
        """It said "file missing, empty, or public IP unknown" - three
        different problems in one sentence, which is how an operator ends up
        looking for a file that is perfectly present."""
        row = config.dcc_queue["someuser"][0]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            dcc.start_dcc_send(None, "someuser", row["path"], row["file"],
                               "#chan", row)

        self.assertIn("the address, not the file", buffer.getvalue())

    def test_a_genuinely_missing_file_is_still_charged(self):
        """The hot loop this budget exists to bound: without it the same
        unreadable entry was re-selected every three seconds for ever, with no
        counter and no way out."""
        # A PUBLIC-LOOKING address, because is_offerable_to_strangers()
        # correctly refuses the documentation ranges - 203.0.113.x is reserved
        # and unreachable from a real network, which is the other half of this
        # same branch. Never dialled: the send aborts on the missing file
        # first. The precondition is asserted rather than assumed, so this
        # cannot quietly start testing the address branch again.
        self.set_config(MY_IP_OR_DOCK="8.8.8.8")
        self.assertTrue(dcc.is_offerable_to_strangers("8.8.8.8"),
                        "fixture invariant: this test needs an address the "
                        "send path accepts, or it tests the wrong branch")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            row0 = config.dcc_queue["someuser"][0]
            dcc.start_dcc_send(None, "someuser", row0["path"], row0["file"],
                               "#chan", row0)

        row = self.row()
        self.assertTrue(row is None or row.get("send_fails"),
                        "a dead row is no longer charged, so nothing bounds "
                        "its re-selection")
        # And it says WHICH of the two things went wrong. The old message
        # named three at once - "file missing, empty, or public IP unknown" -
        # which is how an operator ends up hunting for a file that is present.
        self.assertIn("file missing or empty", buffer.getvalue())


class ARehashWaitsForTransfersToFinish(DCCoreTestCase):
    """From #310:

        When rehash is requested check if dcc send is currently sending,
        pause dcc after a complete send. Do the rehash and when it's finished
        restart queue.

    A reload swaps the modules a running transfer is executing inside, so the
    safe order is: stop starting new ones, let the ones in flight finish,
    reload, start again.
    """

    def setUp(self):
        super().setUp()
        self.set_config(active_transfers=[], transfers_paused=False,
                        REHASH_TRANSFER_WAIT=120)

    def test_a_quiet_bot_does_not_wait_at_all(self):
        slept = []

        went_quiet = dcc.wait_for_transfers_to_finish(sleep=slept.append)

        self.assertTrue(went_quiet)
        self.assertEqual(slept, [], "an idle bot was made to wait")

    def test_new_sends_are_held_while_it_waits(self):
        """"Pause dcc after a complete send" - the pause has to be up before
        the wait begins, or a send started during the wait is one the reload
        lands in the middle of."""
        config.active_transfers.append({"user": "someone", "file": "T.flac"})
        ticks = []

        def stop_after_one(_seconds):
            ticks.append(1)
            self.assertTrue(dcc.transfers_are_paused(),
                            "sends were not held while waiting")
            config.active_transfers.clear()

        self.assertTrue(dcc.wait_for_transfers_to_finish(sleep=stop_after_one))
        self.assertEqual(len(ticks), 1)

    def test_it_waits_until_the_transfer_ends_and_then_stops(self):
        config.active_transfers.append({"user": "someone", "file": "T.flac"})
        ticks = []

        def finish_on_the_third(_seconds):
            ticks.append(1)
            if len(ticks) == 3:
                config.active_transfers.clear()

        self.assertTrue(dcc.wait_for_transfers_to_finish(sleep=finish_on_the_third))
        self.assertEqual(len(ticks), 3)

    def test_a_stuck_transfer_does_not_block_the_rehash_for_ever(self):
        """A transfer can sit idle for as long as the far end keeps its socket
        open. An admin typing !rehash and getting silence is worse than one
        whose transfer was interrupted - at least the second knows what
        happened."""
        config.active_transfers.append({"user": "stuck", "file": "T.flac"})
        ticks = []

        def counted(_seconds):
            # The loop is bounded HERE as well as by the timeout, so a broken
            # timeout fails this test instead of hanging the whole suite - a
            # mutation run found out the hard way that "caught" and "never
            # returns" look identical from outside.
            ticks.append(1)
            if len(ticks) > 20:
                raise AssertionError("the wait is not bounded by its timeout")

        went_quiet = dcc.wait_for_transfers_to_finish(timeout=0, sleep=counted)

        self.assertFalse(went_quiet)

    def test_the_timeout_says_what_it_did(self):
        config.active_transfers.append({"user": "stuck", "file": "T.flac"})
        said = []

        ticks = []

        def counted(_seconds):
            ticks.append(1)
            if len(ticks) > 20:
                raise AssertionError("the wait is not bounded by its timeout")

        dcc.wait_for_transfers_to_finish(timeout=0, sleep=counted,
                                         log=said.append)

        self.assertIn("reloading anyway", chr(10).join(said))

    def test_the_message_does_not_claim_the_list_is_rebuilding(self):
        """update_inprogress means "the list is being rebuilt" and its notice
        says so. Telling somebody that when it is not true is the kind of small
        untruth that makes every other message less believable."""
        # Scoped to the function that actually answers a USER, not to the
        # first `if transfers_are_paused():` in the file. The gate now exists
        # in two places - here, where a new request is refused with a notice,
        # and in check_queue_and_send(), which simply returns because there is
        # nobody to tell - and a whole-file split() read the wrong one the
        # moment the second was added.
        import inspect

        # Two levels of scoping, and both are load-bearing. The FUNCTION,
        # because the gate now exists in two places - here, where a user is
        # refused with a notice, and in check_queue_and_send(), which just
        # returns since there is nobody to tell - and a whole-file split()
        # read the wrong one as soon as the second appeared. Then the BRANCH,
        # because this same function legitimately carries the rebuilding
        # message for update_inprogress, which is a different condition with
        # a different, true notice.
        function = inspect.getsource(dcc.handle_download_request)
        self.assertIn("if transfers_are_paused():", function)
        branch = function.split("if transfers_are_paused():", 1)[1][:600]

        self.assertIn("reloading its configuration", branch)
        self.assertNotIn("MasterList is currently rebuilding", branch)

    def _queue_one_dispatchable_file(self):
        """A queue row this dispatcher will really act on.

        Every field here is load-bearing, and the first version of this test
        had none of them: it queued a row the dispatcher rejected for
        unrelated reasons, so it passed with the gate REMOVED. Mutation
        testing is what exposed that. The control below is the guard against
        it happening again."""
        import io as io_mod

        served = os.path.join(self.make_tree().music, "a.flac")
        os.makedirs(os.path.dirname(served), exist_ok=True)
        with io_mod.open(served, "wb") as handle:
            handle.write(b"x" * 1024)
        self.set_config(active_transfers=[], frozen_queues={},
                        channel_users={"#chan": {"alice"}},
                        bot_joined_channel=True, MAX_DCC_SLOTS=3,
                        dcc_queue={"alice": [{"path": served, "file": "a.flac",
                                              "size": 1024}]})
        self.sent = []
        real_send = dcc.start_dcc_send
        dcc.start_dcc_send = lambda *a, **k: self.sent.append(a)
        self.addCleanup(lambda: setattr(dcc, "start_dcc_send", real_send))

    def test_the_dispatcher_really_does_dispatch_this_row(self):
        """THE CONTROL, and it is the important one. If the fixture stops
        dispatching for some unrelated reason, the pause test below passes
        against a daemon with no gate at all."""
        self._queue_one_dispatchable_file()
        config.transfers_paused = False

        dcc.check_queue_and_send(None, "alice")

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(config.active_transfers), 1)

    def test_a_paused_bot_dispatches_nothing_from_the_queue(self):
        """The gate the wait's own log line promises: "No new sends will
        start."

        It had exactly one reader - handle_download_request(), which turns
        away a NEW request from a user. Nothing stopped the DISPATCHER from
        promoting rows already queued, which is what actually claims a slot
        and starts a send."""
        self._queue_one_dispatchable_file()
        config.transfers_paused = True
        self.addCleanup(lambda: setattr(config, "transfers_paused", False))

        dcc.check_queue_and_send(None, "alice")

        self.assertEqual(self.sent, [],
                         "the dispatcher started a send while the bot was "
                         "quiesced for a rehash")
        self.assertEqual(len(config.active_transfers), 0,
                         "the dispatcher claimed a slot while quiesced")

    def test_the_wait_converges_once_the_dispatcher_honours_it(self):
        """Why it matters. Every completing transfer re-arms a fallback that
        calls back into the dispatcher, so without the gate a freed slot is
        refilled inside the very wait that is trying to drain it - and the
        wait can only ever time out on a busy bot."""
        config.dcc_queue["bob"] = [{"path": "/tmp/b.flac", "filename": "b.flac"}]
        self.addCleanup(config.dcc_queue.pop, "bob", None)
        config.active_transfers.append({"user": "bob", "file": "inflight.flac"})

        ticks = []

        def finish_after_one_tick(_seconds):
            ticks.append(1)
            # The in-flight transfer ends, and the queue trigger fires - which
            # is exactly the moment the slot used to be refilled.
            if len(ticks) == 1:
                config.active_transfers.clear()
                dcc.check_queue_and_send(None, "bob")
            if len(ticks) > 20:
                raise AssertionError("the wait never converged")

        went_quiet = dcc.wait_for_transfers_to_finish(
            timeout=60, sleep=finish_after_one_tick, log=lambda _m: None)

        self.assertTrue(went_quiet,
                        "the queue refilled the slot inside the wait")

    def test_a_paused_bot_does_not_start_new_cross_bot_fetches(self):
        """The SEND side is not the whole of "busy". A fetch never appears in
        config.active_transfers - it has its own queue - so the wait would
        report a quiet bot while this dispatcher was still putting fresh
        requests into the channel, each bringing back an inbound DCC SEND
        landing in the reload window."""
        import dcc_fetch

        config.transfers_paused = True
        self.addCleanup(lambda: setattr(config, "transfers_paused", False))
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=3,
                        fetch_feature_disabled=False, CHANNEL="#chan")
        dcc_fetch.enqueue_fetch("SomeBot", "Track.flac")

        dcc_fetch.check_fetch_queue()

        states = [row.get("state") for row in config.fetch_queue.values()]
        self.assertEqual(states, ["pending"],
                         "a fetch was dispatched while the bot was quiesced")

    def test_fetches_move_again_once_resumed(self):
        """Control: the gate must not wedge the fetch queue shut."""
        import dcc_fetch

        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=3,
                        fetch_feature_disabled=False, CHANNEL="#chan")
        dcc_fetch.enqueue_fetch("SomeBot", "Track.flac")
        config.transfers_paused = True
        dcc_fetch.check_fetch_queue()

        dcc.resume_transfers()
        dcc_fetch.check_fetch_queue()

        states = [row.get("state") for row in config.fetch_queue.values()]
        self.assertEqual(states, ["offered"])

    def test_resuming_lets_the_queue_move_again(self):
        """The gate must not outlive the rehash. resume_transfers() is called
        even when the wait timed out, and the wake that follows it has to
        find a dispatcher that will actually dispatch."""
        config.transfers_paused = True
        dcc.resume_transfers()

        self.assertFalse(dcc.transfers_are_paused())

    def test_the_rehash_waits_before_it_reloads(self):
        """Order is the whole point: waiting AFTER the reload would mean the
        reload already happened inside somebody's transfer. Read out of the
        source, because driving a real rehash means driving a real socket -
        and asserted as a sequence, since the call alone in the wrong place
        would pass a check for the call alone."""
        with io.open(os.path.join(REPO_ROOT, "commands.py"), encoding="utf-8") as handle:
            # COMMENTS STRIPPED, and the search scoped to the rehash body.
            # Whole-file index() found a COMMENT mentioning
            # reload_modules_in_order() five hundred lines above the call, so
            # the assertion compared the wait against a sentence about the
            # reload rather than the reload itself - and failed while the
            # order was perfectly correct.
            code = chr(10).join(line.split("#", 1)[0]
                                for line in handle.read().splitlines())
        body = code.split("def handle_rehash_request(", 1)[1]

        self.assertIn("wait_for_transfers_to_finish()", body,
                      "the rehash does not wait for transfers at all")
        self.assertLess(body.index("wait_for_transfers_to_finish()"),
                        body.index("reload_modules_in_order()"),
                        "the reload happens before the wait, which is the "
                        "thing the wait exists to prevent")

    def test_the_pause_is_lifted_before_the_queue_is_woken(self):
        """Waking the queue while still paused would have every dispatch
        refused by the gate the wait put up - and the wake is the thing that
        restarts the queue the operator asked for."""
        with io.open(os.path.join(REPO_ROOT, "commands.py"), encoding="utf-8") as handle:
            code = handle.read()

        self.assertLess(code.index("_dcc_resume.resume_transfers()"),
                        code.index("[REHASH-WAKE]"))

    def test_the_pause_does_not_outlive_a_rehash_that_raised(self):
        """Otherwise the bot sits refusing every send for ever, with the only
        clue a notice telling users to try again in a moment.

        BOUNDED BY THE HANDLER, not by a character count. This used to read
        the 800 characters before the message, which is not a property of
        anything: a comment explaining that message moved the anchor several
        hundred lines up the file, and widening the window instead reached
        back far enough to find the SUCCESS path's resume and pass on that -
        with the failure path's resume deleted. A count is not a property.

        Comments are stripped for the reason the sibling test above gives.
        The window is now everything between entering the handler and the
        message it prints, which is exactly the span the resume has to be
        in."""
        with io.open(os.path.join(REPO_ROOT, "commands.py"), encoding="utf-8") as handle:
            code = chr(10).join(line.split("#", 1)[0]
                                for line in handle.read().splitlines())
        body = code.split("def _handle_rehash_request(", 1)[1]
        handler = body.split("except Exception as e:", 1)

        self.assertEqual(len(handler), 2, "the rehash's outer handler has moved")
        failure_path = handler[1].split("[REHASH CRITICAL ERROR]", 1)[0]

        self.assertIn("resume_transfers()", failure_path)

    def test_resume_lets_sends_start_again(self):
        config.transfers_paused = True

        dcc.resume_transfers()

        self.assertFalse(dcc.transfers_are_paused())


class HowMuchGoesOutPerPass(DCCoreTestCase):
    """mIRC calls it the packet size, defaults it to 4 KB, and raising it there
    is noticeable - which is why operators come looking for it here.

    DCCore has always used 64 KB, sixteen times that, and does not wait for the
    receiver to acknowledge each block before sending the next. So the thing
    they are looking for was already on; this only exposes the number.
    """

    def test_the_default_is_what_it_has_always_been(self):
        """Sixteen times mIRC's, so nobody's transfers change speed because
        this became a setting."""
        self.assertEqual(dcc.dcc_block_size(), 65536)

    def test_a_bigger_value_is_honoured(self):
        self.set_config(DCC_BLOCK_SIZE=131072)

        self.assertEqual(dcc.dcc_block_size(), 131072)

    def test_the_menu_is_the_one_mirc_offers(self):
        """4, 8, 16, 32, 64, 128 KB - what an operator is comparing against
        when they say "the same setting mIRC has"."""
        import settings_file

        self.assertEqual(
            settings_file.CHOICES["DCC_BLOCK_SIZE"],
            ("4096", "8192", "16384", "32768", "65536", "131072"))

    def test_a_chosen_number_comes_back_as_a_number(self):
        """Every choice was a string until this one. Returning "65536" for a
        setting declared int would hand the send loop a str to read() with -
        type drift that only shows up on the one path nobody exercised."""
        import settings_file

        value = settings_file.coerce("DCC_BLOCK_SIZE", "131072", 65536)

        self.assertEqual(value, 131072)
        self.assertIsInstance(value, int)

    def test_a_boolean_choice_would_not_become_one_and_zero(self):
        """bool IS an int in Python, so `isinstance(default, int)` is True for
        True and False - and a choice of ("yes", "no") declared against a bool
        default would come back as 1 and 0 without the guard above it.

        No such choice exists today, which is exactly why this test adds one:
        a guard nothing can falsify is a guard nobody can trust, and this
        hazard is a language subtlety the next person to add a choice will
        walk straight into."""
        import settings_file

        original = dict(settings_file.CHOICES)
        settings_file.CHOICES["A_MADE_UP_FLAG"] = ("yes", "no")
        self.addCleanup(lambda: (settings_file.CHOICES.clear(),
                                 settings_file.CHOICES.update(original)))

        value = settings_file.coerce("A_MADE_UP_FLAG", "yes", False)

        self.assertEqual(value, "yes")
        self.assertNotIsInstance(value, int)

    def test_a_string_choice_is_still_a_string(self):
        """bool is an int in Python, so the conversion has to be told apart
        from a future True/False choice as well as from these."""
        import settings_file

        self.assertEqual(settings_file.coerce("LIST_FORMAT", "ZIP", "zip"), "zip")
        self.assertEqual(settings_file.coerce("THEME", "forest", "classic"), "forest")

    def test_something_off_the_menu_is_refused(self):
        import settings_file

        with self.assertRaises(ValueError):
            settings_file.coerce("DCC_BLOCK_SIZE", "99999", 65536)

    def test_the_dashboard_offers_it_as_a_list(self):
        import webserver

        field = webserver._settings_field("DCC_BLOCK_SIZE", int, 65536)

        self.assertEqual(field["choices"],
                         ["4096", "8192", "16384", "32768", "65536", "131072"])


class TheSocketSendBuffer(DCCoreTestCase):
    """What actually bounds a transfer on a fast, distant link - and it is not
    the packet size.

    Throughput on TCP is the bandwidth-delay product: bytes in flight =
    bandwidth x round-trip time. At 100 Mbps and 100 ms RTT that is about
    1.25 MB, and a 64 KB send buffer caps the transfer at roughly 5 Mbps
    however big each write is.
    """

    class FakeSocket(object):
        def __init__(self, fail=False):
            self.options = []
            self.fail = fail

        def setsockopt(self, level, option, value):
            if self.fail:
                raise OSError("refused")
            self.options.append((level, option, value))

    def test_zero_means_the_default_for_the_platform(self):
        """This used to assert that zero touched nothing at all, on the
        reasoning that setting SO_SNDBUF disables the OS's own auto-tuning
        "on both platforms". Half right, and the class docstring above had
        already worked out what the other half would cost.

        It holds on Linux, where tcp_wmem grows the buffer to fit the
        connection. On Windows, "leave it alone" is a fixed 64 KB - which a
        beta measured at 3.19 MB/s over a 20.6 ms link, against 30.4 MB/s
        from an OmenServe bot on the same machine to the same person. So
        zero now means the default for the platform, and only one platform
        has one.

        See tests/test_send_buffer_is_not_a_64kb_ceiling.py for the
        measurements and for the rest of this behaviour."""
        import platform_compat

        self.set_config(DCC_SEND_BUFFER=0)
        sock = self.FakeSocket()

        dcc._apply_send_buffer(sock)

        if platform_compat.IS_WINDOWS:
            self.assertEqual([value for _l, _o, value in sock.options],
                             [4 * 1024 * 1024])
        else:
            self.assertEqual(sock.options, [])

    def test_a_requested_size_is_set(self):
        import socket as socket_mod

        self.set_config(DCC_SEND_BUFFER=262144)
        sock = self.FakeSocket()

        dcc._apply_send_buffer(sock)

        self.assertEqual(
            sock.options,
            [(socket_mod.SOL_SOCKET, socket_mod.SO_SNDBUF, 262144)])

    def test_a_kernel_that_refuses_does_not_fail_the_transfer(self):
        """The kernel is entitled to refuse or to round the value, and a
        socket option that cannot be set is not a reason to fail a transfer
        that would otherwise work."""
        self.set_config(DCC_SEND_BUFFER=262144)
        said = []

        dcc._apply_send_buffer(self.FakeSocket(fail=True), log=said.append)

        self.assertIn("Continuing with the OS default", chr(10).join(said))

    def test_nonsense_is_ignored_rather_than_raised(self):
        self.set_config(DCC_SEND_BUFFER="lots")
        sock = self.FakeSocket()

        dcc._apply_send_buffer(sock)

        self.assertEqual(sock.options, [])

    def test_the_transfer_path_applies_it(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            code = handle.read()

        self.assertIn("_apply_send_buffer(conn)", code)

    def test_a_value_that_would_spin_the_loop_is_raised(self):
        """0 or a negative makes read() return nothing and the loop turn into
        a syscall storm - and neither is worth a startup error when the honest
        thing is to use the nearest usable number and get on with it."""
        for value in (0, -5, 100):
            with self.subTest(value=value):
                self.set_config(DCC_BLOCK_SIZE=value)
                self.assertEqual(dcc.dcc_block_size(), dcc.MIN_DCC_BLOCK_SIZE)

    def test_a_value_that_would_eat_the_machine_is_capped(self):
        """This is multiplied by the number of concurrent transfers to give the
        memory held in send buffers, so a mistyped 500000000 is half a gigabyte
        per slot."""
        self.set_config(DCC_BLOCK_SIZE=500000000)

        self.assertEqual(dcc.dcc_block_size(), dcc.MAX_DCC_BLOCK_SIZE)

    def test_something_that_is_not_a_number_falls_back(self):
        self.set_config(DCC_BLOCK_SIZE="fast please")

        self.assertEqual(dcc.dcc_block_size(), 65536)

    def test_the_send_loop_uses_it(self):
        """Read out of the source: driving a real transfer needs a peer on a
        socket, and what matters is that the loop reads the setting rather
        than the literal it replaced."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            code = handle.read()

        self.assertIn("block = dcc_block_size()", code)
        self.assertIn("f.read(block)", code)
        self.assertNotIn("f.read(65536)", code,
                         "the send loop still has the number hardcoded")

    def test_it_is_resolved_once_per_transfer_not_once_per_pass(self):
        """A getattr in the inner loop of a 4 GB send is a million lookups for
        one answer that cannot change mid-file."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            code = handle.read()
        loop = code.split("block = dcc_block_size()", 1)[1].split("break", 1)[0]

        self.assertNotIn("dcc_block_size()", loop)


if __name__ == "__main__":
    unittest.main()
