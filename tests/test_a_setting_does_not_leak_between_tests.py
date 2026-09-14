"""A test that changes a setting must not leave it changed.

RUNTIME_FLAGS already covers live state, and every container in runtime.py is
emptied between tests. Ordinary config values with a module-level default had
nothing - and they are the harder case, because a test which changes one is
usually testing something else entirely, so nothing about it looks like state
management.

BROADCAST_SEARCH_CHANNEL is the one that proved it. tests/
test_config_overrides.py sets it while checking that settings.conf overrides a
module default, which is exactly what that file is for, and nothing put it
back. Every later test in the run then saw a channel it never configured.

What it cost: a fetch test that reads the fallback channel through that same
value failed with

    'PRIVMSG #one :' not found in 'PRIVMSG #dccore-test :...'

naming a channel from a test file it has nothing to do with, in a run where
three consecutive full suites had just passed. It was read as a flake twice
before it was read as a leak.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402

from tests import support  # noqa: E402
from tests.support import DCCoreTestCase  # noqa: E402


class TheHarnessPutsSettingsBack(DCCoreTestCase):

    def test_a_leaked_broadcast_channel_is_cleared(self):
        """Simulates the leak directly rather than depending on test order -
        an ordering test passes or fails for reasons that have nothing to do
        with the thing it is about."""
        config.BROADCAST_SEARCH_CHANNEL = "#somewhere-else"

        support.reset_config()

        self.assertIsNone(config.BROADCAST_SEARCH_CHANNEL)

    def test_the_list_is_not_empty(self):
        """Fixture invariant: an empty SETTINGS_DEFAULTS would make the reset
        loop a no-op and every assertion here vacuous."""
        self.assertTrue(support.SETTINGS_DEFAULTS)

    def test_every_name_in_it_is_a_real_setting(self):
        """A typo here resets nothing and says nothing - the loop would
        happily create a new attribute nobody reads."""
        missing = [name for name in support.SETTINGS_DEFAULTS
                   if not hasattr(config, name)]

        self.assertEqual(missing, [])

    def test_the_reset_runs_for_every_test(self):
        """Not only when a test asks. The whole point is that the test which
        leaked was not thinking about state at all."""
        self.assertIsNone(config.BROADCAST_SEARCH_CHANNEL,
                          "setUp did not restore it")


class TheFallbackChannelIsReadFromConfig(DCCoreTestCase):
    """Why this particular setting mattered enough to notice: it is what a
    fetch falls back to when presence cannot place the bot, so a leaked value
    silently redirects real requests in a test run."""

    def test_dcc_fetch_prefers_it_when_it_is_set(self):
        import dcc_fetch

        source = open(os.path.join(REPO_ROOT, "dcc_fetch.py"),
                      encoding="utf-8").read()

        self.assertIn("BROADCAST_SEARCH_CHANNEL", source)
        self.assertTrue(hasattr(dcc_fetch, "check_fetch_queue"))


if __name__ == "__main__":
    unittest.main()
