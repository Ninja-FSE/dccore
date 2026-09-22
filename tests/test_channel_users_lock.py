"""Regression coverage for the config.channel_users race.

config.channel_users (a dict of sets, tracking who is currently seen in each
IRC channel) used to be mutated from the IRC read thread on every JOIN/PART/
QUIT/353 with no locking at all, while dcc.py and commands.py read or mutated
the very same structure from other threads (queue dispatch, !rehash, the
automatic channel-sync loop) - also unlocked. Adding or deleting a CHANNEL KEY
(as irc.py's JOIN handler and commands.py's channel-sync loop both do) while
another thread iterates the dict raises "RuntimeError: dictionary changed
size during iteration" - and short of that, a reader can simply observe a
half-updated channel roster.

runtime.channel_users_lock() is the fix: one shared lock, used at every touch
point in irc.py, dcc.py and commands.py. These tests exercise the real,
already-locked dcc.user_is_present_in_ram() against a writer thread that adds
and removes channel keys exactly the way irc.py's JOIN handler and
commands.py's channel-sync loop do (through the same lock), and separately
prove, with the interleaving forced rather than left to the scheduler, that a
channel added in the middle of an iteration raises when left unlocked and
waits its turn when locked - so the passing test above is shown to depend on
the fix, not merely be compatible with it.
"""

import threading
import time
import unittest

from tests.support import DCCoreTestCase

import defaults as config
import dcc
import runtime


class ChannelUsersLockIsShared(DCCoreTestCase):
    """runtime.channel_users_lock() must always resolve to ONE object.

    A second, independently-constructed Lock() would make every call site
    correctly implemented and still achieve nothing: two callers each holding
    a different lock never exclude each other. This is the same failure mode
    already fixed once in list_fetch.py's _lock() (see FallbackLockIsShared
    in test_list_fetch.py) and deliberately avoided here from the start.
    """

    def test_repeated_calls_return_one_object(self):
        self.assertIs(runtime.channel_users_lock(), runtime.channel_users_lock())

    def test_the_fallback_survives_config_not_having_the_attribute_yet(self):
        if hasattr(config, "channel_users_lock"):
            del config.channel_users_lock
        first = runtime.channel_users_lock()
        second = runtime.channel_users_lock()
        self.assertIs(first, second,
                      "the fallback handed out a fresh lock per call, so "
                      "concurrent callers never actually excluded each other")

    def test_config_channel_users_lock_wins_once_it_exists(self):
        config.channel_users_lock = threading.Lock()
        self.assertIs(runtime.channel_users_lock(), config.channel_users_lock)


def _add_remove_channel_keys(stop, deadline, locked):
    """Mirrors irc.py's JOIN handler (new channel key) and commands.py's
    channel-sync loop (`del config.channel_users[chan]`) - the two real call
    sites that change the DICT's size, which is what a concurrent iterator
    actually detects.
    """
    i = 0
    while not stop.is_set() and time.monotonic() < deadline:
        chan = f"#race{i % 8}"

        def _add():
            config.channel_users[chan] = {"someuser"}

        def _remove():
            if chan in config.channel_users:
                del config.channel_users[chan]

        if locked:
            with runtime.channel_users_lock():
                _add()
            with runtime.channel_users_lock():
                _remove()
        else:
            _add()
            _remove()
        i += 1


class ConcurrentChannelKeyChurnAgainstRamCheck(DCCoreTestCase):
    """Races channel add/delete against dcc.user_is_present_in_ram()."""

    DEADLINE_SECONDS = 3

    def _reader(self, stop, errors, deadline):
        try:
            while not stop.is_set() and time.monotonic() < deadline:
                dcc.user_is_present_in_ram("someuser")
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    def test_concurrent_channel_churn_and_ram_check_do_not_corrupt_state(self):
        stop = threading.Event()
        errors = []
        deadline = time.monotonic() + self.DEADLINE_SECONDS

        writer = threading.Thread(
            target=_add_remove_channel_keys, args=(stop, deadline, True), daemon=True)
        reader = threading.Thread(
            target=self._reader, args=(stop, errors, deadline), daemon=True)
        writer.start()
        reader.start()
        writer.join(timeout=self.DEADLINE_SECONDS + 5)
        stop.set()
        reader.join(timeout=5)

        self.assertFalse(writer.is_alive(), "writer thread never finished - possible deadlock")
        self.assertFalse(reader.is_alive(), "reader thread never finished - possible deadlock")
        self.assertEqual(errors, [],
                         f"concurrent access raised despite the shared lock: {errors!r}")

    def _iterate_while_another_thread_adds_a_channel(self, locked):
        """Hold an iteration open, let a writer add a channel key, resume.

        Deterministic on purpose (#596): the control used to churn an unlocked
        dict for three seconds and assert that the scheduler happened to
        interleave a writer inside an iteration - which a lightly loaded runner
        does not always do, and main went red for a change that touched
        nothing near it. Here the interleaving is forced with events, so the
        outcome does not depend on the scheduler at all.
        """
        config.channel_users.clear()
        config.channel_users["#race0"] = {"someuser"}
        inside_the_loop = threading.Event()
        writer_done = threading.Event()
        errors = []

        def writer():
            inside_the_loop.wait(5)
            if locked:
                with runtime.channel_users_lock():
                    config.channel_users["#race1"] = {"someuser"}
            else:
                config.channel_users["#race1"] = {"someuser"}
            writer_done.set()

        def reader():
            try:
                def walk():
                    for users_set in config.channel_users.values():
                        for known_user in users_set:
                            str(known_user).lower()
                        # The writer runs here, in the middle of the iteration
                        # - unless the lock keeps it out until the loop is over.
                        inside_the_loop.set()
                        writer_done.wait(0.5 if locked else 5)
                if locked:
                    with runtime.channel_users_lock():
                        walk()
                else:
                    walk()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, daemon=True),
                   threading.Thread(target=reader, daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads), "a thread never finished")
        return errors

    def test_without_the_lock_a_channel_added_mid_iteration_raises(self):
        """Control: the same reader and writer, nothing serialising them.

        Shows the tests above are not vacuously passing - the churn
        dcc.user_is_present_in_ram() survives is exactly what raises
        "dictionary changed size during iteration" when nothing serialises it.
        """
        errors = self._iterate_while_another_thread_adds_a_channel(locked=False)

        self.assertEqual(len(errors), 1, errors)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("changed size during iteration", str(errors[0]))

    def test_with_the_lock_the_writer_waits_for_the_iteration_to_finish(self):
        errors = self._iterate_while_another_thread_adds_a_channel(locked=True)

        self.assertEqual(errors, [])
        self.assertIn("#race1", config.channel_users, "the writer never got its turn after the loop")


if __name__ == "__main__":
    unittest.main()
