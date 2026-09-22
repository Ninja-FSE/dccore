"""MSG_DELAY and DEBUG_MSG_DELAY were set directly and never restored, so
every later test ran on a 10 ms pacer (audit L3, #667).

Six setUps - two in test_a_shared_outbound_pace.py, and one each in
test_reconnect.py, test_the_vip_lane_gets_one_slot_per_pass.py,
test_the_pump_waits_for_the_joins_to_land.py and test_announce_output.py -
assigned config.MSG_DELAY (0.01 / 0.05) or config.DEBUG_MSG_DELAY (0.01)
without set_config() or a cleanup, and reset_config() did not reset them,
so the shipped 5.0 s / 0 never came back for the rest of the process. The
comment in the first of them said other tests would not see its tiny pace;
it only replaced the pacer object. Nothing failed today - every test that
makes more than one paced send pins the pace itself - but a test written
later that did not would pass in the full run at 10 ms and stall five
seconds per line run on its own.

Both names are in support.SETTINGS_DEFAULTS now, at the shipped values, and
the six setUps go through set_config().
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402

from tests import support  # noqa: E402
from tests import test_a_shared_outbound_pace as pace  # noqa: E402

TESTS_DIR = os.path.join(REPO_ROOT, "tests")

# A direct assignment to either delay, on the real config module: what the
# audit found. `self.set_config(MSG_DELAY=...)` does not match.
DIRECT_ASSIGNMENT = re.compile(r"^\s*(?:self\.)?config\.(?:DEBUG_)?MSG_DELAY\s*=", re.MULTILINE)


def shipped(name):
    with io.open(os.path.join(REPO_ROOT, "defaults.py"), encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(name + ":") or line.startswith(name + " "):
                return float(line.split("=", 1)[1].split("#", 1)[0].strip())
    raise AssertionError("%s is not in defaults.py" % name)


class TheHarness(unittest.TestCase):

    def test_reset_config_puts_the_shipped_pace_back(self):
        config.MSG_DELAY = 0.01
        config.DEBUG_MSG_DELAY = 0.01

        support.reset_config()

        self.assertEqual((config.MSG_DELAY, config.DEBUG_MSG_DELAY), (shipped("MSG_DELAY"), shipped("DEBUG_MSG_DELAY")))

    def test_the_harness_defaults_are_the_shipped_ones(self):
        """The harness names the values literally; a release that retunes
        the pace must retune this too, or every test would run on a stale
        pace and never notice."""
        self.assertEqual(support.SETTINGS_DEFAULTS["MSG_DELAY"], shipped("MSG_DELAY"))
        self.assertEqual(support.SETTINGS_DEFAULTS["DEBUG_MSG_DELAY"], shipped("DEBUG_MSG_DELAY"))


class ThePacedTests(unittest.TestCase):
    """The class the audit measured: its setUp sets 50 ms / 10 ms, and after
    its tearDown the shipped pace is back."""

    def test_the_tiny_pace_does_not_outlive_the_test(self):
        support.reset_config()
        # Any of its tests: setUp and tearDown are what is under test here.
        a_test = [n for n in dir(pace.TheCombinedOutboundRateIsCapped) if n.startswith("test")][0]
        case = pace.TheCombinedOutboundRateIsCapped(a_test)
        case.setUp()
        try:
            self.assertEqual((config.MSG_DELAY, config.DEBUG_MSG_DELAY), (0.05, 0.01))
        finally:
            case.tearDown()
            case.doCleanups()

        self.assertEqual((config.MSG_DELAY, config.DEBUG_MSG_DELAY), (shipped("MSG_DELAY"), shipped("DEBUG_MSG_DELAY")))

    def test_no_test_sets_the_pace_directly(self):
        offenders = []
        for name in sorted(os.listdir(TESTS_DIR)):
            if not name.endswith(".py") or name == os.path.basename(__file__):
                continue
            with io.open(os.path.join(TESTS_DIR, name), encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle.read().splitlines(), 1):
                    if DIRECT_ASSIGNMENT.match(line):
                        offenders.append("%s:%d" % (name, number))

        self.assertEqual(offenders, [], "set the pace through self.set_config(): %s" % offenders)

    def test_the_sweep_reads_what_the_audit_found(self):
        self.assertTrue(DIRECT_ASSIGNMENT.match("        config.MSG_DELAY = 0.05"))
        self.assertTrue(DIRECT_ASSIGNMENT.match("        self.config.DEBUG_MSG_DELAY = 0.01"))
        self.assertFalse(DIRECT_ASSIGNMENT.match("        self.set_config(MSG_DELAY=0.01)"))
        self.assertFalse(DIRECT_ASSIGNMENT.match("        elapsed, 7 * config.MSG_DELAY * 0.8,"))


if __name__ == "__main__":
    unittest.main()
