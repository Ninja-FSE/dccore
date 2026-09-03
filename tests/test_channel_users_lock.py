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
prove that the identical add/delete-key workload reliably corrupts iteration
when left unlocked - so the passing test above is shown to depend on the fix,
not merely be compatible with it.
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

    def test_without_the_lock_the_same_workload_corrupts_state(self):
        """Control: the identical add/delete-key workload, left unlocked.

        Demonstrates the test above is not vacuously passing - the same
        churn that dcc.user_is_present_in_ram() now survives reliably raises
        "dictionary changed size during iteration" when nothing serialises
        it, which is exactly the bug this PR fixes.
        """
        stop = threading.Event()
        errors = []
        deadline = time.monotonic() + self.DEADLINE_SECONDS

        def unlocked_reader():
            try:
                while not stop.is_set() and time.monotonic() < deadline:
                    for users_set in config.channel_users.values():
                        for known_user in users_set:
                            str(known_user).lower()
            except Exception as exc:
                errors.append(exc)
            finally:
                stop.set()

        writer = threading.Thread(
            target=_add_remove_channel_keys, args=(stop, deadline, False), daemon=True)
        reader = threading.Thread(target=unlocked_reader, daemon=True)
        writer.start()
        reader.start()
        writer.join(timeout=self.DEADLINE_SECONDS + 5)
        stop.set()
        reader.join(timeout=5)

        self.assertTrue(len(errors) > 0,
                        "the unlocked control workload finished without ever "
                        "raising - this test would no longer prove the fix "
                        "prevents anything; it needs a heavier workload")


if __name__ == "__main__":
    unittest.main()
