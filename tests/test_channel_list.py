"""JOIN is stricter than the setting looks.

WHAT WENT WRONG

RFC 2812 is `JOIN <channel>{,<channel>} [<key>{,<key>}]` - SPACE-separated
parameters. So a space anywhere in the channel list ends it. Sending

    JOIN #one, #two, #three

joins "#one" and hands the server "#two," as a channel KEY; the rest is
discarded. Nothing about that is an error, so nothing reports one.

config.CHANNEL was passed to JOIN verbatim. An operator who wrote
"#a, #b, #c" - which reads naturally, and is how configure.py echoes the value
back at them - joined their first channel and no others. Reported from a real
install: six channels configured, one joined, and the log said "Activating the
advert despite 5 unconfirmed channel(s)" without ever connecting the two.

The advert loop was fine, because announce.py strips each entry as it goes.
Only the JOIN was not, which is why the bot looked half-working rather than
broken.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import irc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def join_argument():
    """Every channel the connect path asks for, comma-joined - the string this
    file has always been about.

    irc.join_target_list() used to BE that string, and was deleted in #510
    when the JOIN stopped being a single line: the server was truncating it,
    and the channels lost were whatever sat at the end. The property here is unchanged
    and is now asserted one step closer to the wire - on the payloads actually
    sent, rather than on a string that was about to become one.
    """
    return ",".join(irc.join_batches(irc.channels_we_should_be_in()))


class TheJoinLineNeverContainsASpace(DCCoreTestCase):
    """The property, stated as the protocol states it. Everything else in this
    file is a way of arriving at this one."""

    def setUp(self):
        super().setUp()
        # THIS FILE IS ABOUT CHANNEL, and channels_we_should_be_in() also
        # carries DEBUG_CHANNEL - which the harness sets to a real value, so
        # every expected string below would have it appended. The debug
        # channel's own place in the JOIN is tests/test_a_join_nobody_checked.py's
        # business; pinning it blank here keeps each file asserting one thing.
        self.set_config(DEBUG_CHANNEL="")

    def test_spaces_after_commas_are_tolerated(self):
        self.set_config(CHANNEL="#Alpha, #bravo, #charlie")

        self.assertEqual(join_argument(),
                         "#Alpha,#bravo,#charlie")
        self.assertNotIn(" ", join_argument())

    def test_the_real_reported_configuration(self):
        """Six channels, one joined - the shape of the install that found it,
        with invented names.

        The names here used to be that operator's real ones, and the docstring
        said so. What made it a leak was not the report: it was that a later
        partial scrub stripped a common prefix off each and left the
        distinctive tails, in the operator's own configured ORDER, under a
        sentence telling every reader they were genuine. Six invented names
        reproduce the case exactly - what this test needs is the count, the
        spacing and one capital."""
        self.set_config(CHANNEL="#Alpha, #bravo, #charlie, "
                                "#delta-two, #echo, #foxtrot")

        self.assertEqual(len(irc.configured_channels()), 6)
        self.assertNotIn(" ", join_argument())

    def test_whitespace_of_every_kind_is_removed(self):
        self.set_config(CHANNEL="  #one ,\t#two,\n#three  ")

        self.assertEqual(join_argument(), "#one,#two,#three")

    def test_empty_entries_are_dropped(self):
        """A trailing comma is the commonest edit-by-hand mistake, and an empty
        channel in a JOIN is a malformed line rather than a no-op."""
        self.set_config(CHANNEL="#one,,#two,")

        self.assertEqual(join_argument(), "#one,#two")

    def test_order_is_preserved(self):
        """The first channel is not arbitrary: defaults.py derives
        BROADCAST_SEARCH_CHANNEL from it."""
        self.set_config(CHANNEL="#zebra, #alpha, #middle")

        self.assertEqual(irc.configured_channels(),
                         ["#zebra", "#alpha", "#middle"])

    def test_case_is_left_alone(self):
        """Channel names are case-insensitive on the wire, but the operator
        typed what they typed and it appears in the advert."""
        self.set_config(CHANNEL="#Alpha, #bravo")

        self.assertEqual(irc.configured_channels()[0], "#Alpha")


class TheOrdinaryCasesStillWork(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        # THIS FILE IS ABOUT CHANNEL, and channels_we_should_be_in() also
        # carries DEBUG_CHANNEL - which the harness sets to a real value, so
        # every expected string below would have it appended. The debug
        # channel's own place in the JOIN is tests/test_a_join_nobody_checked.py's
        # business; pinning it blank here keeps each file asserting one thing.
        self.set_config(DEBUG_CHANNEL="")

    def test_a_list_with_no_spaces_is_unchanged(self):
        """Control. The overwhelmingly common configuration must pass through
        untouched, or this fix would be a change of behaviour rather than a
        repair."""
        self.set_config(CHANNEL="#one,#two,#three")

        self.assertEqual(join_argument(), "#one,#two,#three")

    def test_a_single_channel(self):
        self.set_config(CHANNEL="#only")

        self.assertEqual(join_argument(), "#only")
        self.assertEqual(irc.configured_channels(), ["#only"])

    def test_a_blank_setting_does_not_raise(self):
        """CHANNEL is in settings_file.REQUIRED so the daemon refuses to boot
        without it, but this runs on a config object anything can assign to."""
        for value in ("", "   ", ",", None):
            with self.subTest(value=value):
                self.set_config(CHANNEL=value)

                self.assertEqual(irc.configured_channels(), [])
                self.assertEqual(join_argument(), "")


class TheJoinUsesIt(unittest.TestCase):
    """The fix is only a fix if the JOIN actually calls it. Read out of the
    source rather than retyped: a helper that exists and is not used would
    satisfy every test above."""

    def test_the_join_thread_is_handed_the_normalised_list(self):
        import io

        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("args=(s, channels_we_should_be_in())",
                      source.replace("\n", " ").replace("  ", " "),
                      "the JOIN thread is not being handed "
                      "channels_we_should_be_in() - if it takes config.CHANNEL "
                      "directly, the space bug is back")
        self.assertNotIn("args=(s, config.CHANNEL)", source,
                         "the JOIN is passing the raw setting again")


if __name__ == "__main__":
    unittest.main()
