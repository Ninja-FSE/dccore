"""The escalation ban lasts a fixed time, not "until midnight".

THE DEFECT (#227)

Someone who kept flooding while already muted was banned until local midnight:

    seconds_until_midnight = 86400 - seconds_since_midnight
    config.banned_users[user_key] = now + seconds_until_midnight

So the sentence depended on the clock rather than the offence. Trip it at 00:01
and you were banned for nearly a day; trip it at 23:59 and you were banned for
seconds. Identical behaviour, arbitrary punishment - and the weak end of that
range is the end an actual flooder finds, because they are already there when
the clock rolls over.

Now config.FLOOD_BAN_SECONDS, defaulting to an hour.

WHAT THIS GIVES UP, DELIBERATELY

Midnight expiry accidentally escalated: trip it at 20:00 for four hours, come
back at 22:00 and get two more - each repeat landing in the same shrinking
window, so the evening's punishment kept applying. A flat hour does not do
that. An attacker can trip it, wait, and trip it again indefinitely at a fixed
cost.

That is the right trade at this tier: a determined flooder is a hard-ban case
(!ban, a permanent wildcard), not a mute case. Recorded because it is a real
behaviour change and not an obvious one, and because if a third rung is ever
wanted it should be built deliberately rather than inherited from clock
arithmetic.
"""

import io
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import security  # noqa: E402

import announce  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402


class TheBanLastsWhatTheSettingSays(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.muted_until.clear()
        config.banned_users.clear()
        self.addCleanup(config.muted_until.clear)
        self.addCleanup(config.banned_users.clear)

    def trip(self, user="dave"):
        """Put the user in the "muted and still hammering" state, and flood."""
        config.muted_until[user.lower()] = time.time() + 30
        # Not a context manager - it swaps announce.send_debug and returns the
        # capture list. DCCoreTestCase.setUp's restore_daemon_functions() puts
        # the real one back for the next test.
        silence_debug(announce)
        return security.is_flooding(user)

    def test_the_ban_is_the_configured_length(self):
        self.set_config(FLOOD_BAN_SECONDS=3600)
        before = time.time()

        self.assertTrue(self.trip())

        remaining = config.banned_users["dave"] - before
        self.assertAlmostEqual(remaining, 3600, delta=5)

    def test_a_different_setting_is_honoured(self):
        """Not a hardcoded hour dressed up as a setting."""
        self.set_config(FLOOD_BAN_SECONDS=120)
        before = time.time()

        self.trip()

        self.assertAlmostEqual(config.banned_users["dave"] - before, 120, delta=5)

    def test_the_length_does_not_depend_on_the_time_of_day(self):
        """The defect, stated directly. Two trips at very different clock
        times must produce the same sentence.

        time.localtime is stubbed rather than the clock waited on: the old
        arithmetic read the wall clock, so pinning it is what proves the new
        code does not.
        """
        self.set_config(FLOOD_BAN_SECONDS=3600)
        real_localtime = time.localtime
        durations = []

        for hour in (0, 12, 23):
            time.localtime = (lambda h: (lambda *a: real_localtime(
                time.mktime((2026, 9, 2, h, 59, 0, 0, 0, -1)))))(hour)
            try:
                config.muted_until.clear()
                config.banned_users.clear()
                before = time.time()
                self.trip()
                durations.append(round(config.banned_users["dave"] - before))
            finally:
                time.localtime = real_localtime

        self.assertEqual(len(set(durations)), 1,
                         f"the ban length still varies with the clock: {durations}")


class EveryMessageSaysTheSameThing(DCCoreTestCase):
    """All three used to say "until midnight". The one a stranger reads is the
    one that matters, and it was the one that stayed wrong longest in every
    other case this session."""

    def setUp(self):
        super().setUp()
        config.muted_until.clear()
        config.banned_users.clear()
        self.addCleanup(config.muted_until.clear)
        self.addCleanup(config.banned_users.clear)
        self.set_config(FLOOD_BAN_SECONDS=3600)

    def test_the_user_is_told_the_real_duration(self):
        config.muted_until["dave"] = time.time() + 30
        silence_debug(announce)
        security.is_flooding("dave")

        text = "".join(m for _u, m, *_ in self.oserve.queued)

        self.assertIn("1 hour", text)
        self.assertNotIn("midnight", text)

    def test_the_wording_follows_the_setting(self):
        self.set_config(FLOOD_BAN_SECONDS=900)
        config.muted_until["dave"] = time.time() + 30
        silence_debug(announce)
        security.is_flooding("dave")

        self.assertIn("15 minutes", "".join(m for _u, m, *_ in self.oserve.queued))

    def test_no_message_in_the_module_still_says_midnight(self):
        """Five separate strings said it. Changing the arithmetic and leaving
        them would have left the daemon lying about its own behaviour.

        String CONSTANTS, docstrings excluded - the module explains this
        history in prose on purpose, and a scan that cannot tell a message
        from an explanation would force the explanation out.
        """
        import ast

        with io.open(os.path.join(REPO_ROOT, "security.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)

        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docstrings:
                    continue
                if "midnight" in node.value.lower():
                    offenders.append(f"line {node.lineno}: {node.value[:60]}")

        self.assertEqual(offenders, [],
                         "a message still promises a ban until midnight")


class TheDurationPhrase(unittest.TestCase):
    """format_ban_duration() - the one place every message gets its wording."""

    def test_it_reads_naturally_across_the_range(self):
        cases = {1: "1 second", 45: "45 seconds", 60: "1 minute",
                 900: "15 minutes", 3600: "1 hour", 7200: "2 hours",
                 5400: "1.5 hours"}
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(security.format_ban_duration(seconds), expected)

    def test_it_does_not_produce_a_negative_or_blank_phrase(self):
        """A misconfigured or clock-skewed value must still read as something."""
        for seconds in (0, -1, -3600):
            with self.subTest(seconds=seconds):
                self.assertTrue(security.format_ban_duration(seconds).strip())
                self.assertNotIn("-", security.format_ban_duration(seconds))


if __name__ == "__main__":
    unittest.main()
