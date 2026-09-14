"""A PRIVMSG target is one channel, and the default was a list of them.

WHAT WENT WRONG

dcc.py picked the channel to announce a finished transfer in like this:

    target_chan = next_file.get('channel', config.CHANNEL.split(','))

`.split(',')` returns a LIST. So a queue entry with no 'channel' key handed a
list to start_dcc_send(), which passes it straight to
announce.send_transfer_complete(), which builds

    f"PRIVMSG {channel} :"

and the line that went out was

    PRIVMSG ['#one', '#two'] :Sent Song.flac to nick

A malformed target. The server answers with a numeric nothing here reads, so
the announcement was lost - while the transfer it was announcing had already
succeeded. Nothing raised and nothing logged: the same signature as every
other defect found in this project's real installs.

Latent rather than live: every entry the request path builds does set
'channel'. Reaching it needs an entry from somewhere else, or one restored
from a queue file written before that key existed. The default was the wrong
SHAPE either way, and a default that is only ever correct by accident of
nothing using it is worth removing.

The sibling site at dcc.py's global-queue scan is deliberately NOT changed: it
uses the same expression, but as a list of channels to test membership in, and
it type-switches on str vs list explicitly. There a list is the right answer.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import dcc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheDefaultIsOneChannel(DCCoreTestCase):

    def test_it_is_a_string_not_a_list(self):
        """The whole defect in one assertion."""
        self.set_config(CHANNEL="#one,#two,#three")

        result = dcc.default_announce_channel()

        self.assertIsInstance(result, str)
        self.assertEqual(result, "#one")

    def test_it_is_the_first_configured_channel(self):
        """Same choice defaults.py makes for BROADCAST_SEARCH_CHANNEL, so the
        two cannot disagree about which channel is 'the' one."""
        self.set_config(CHANNEL="#primary,#secondary")

        self.assertEqual(dcc.default_announce_channel(), "#primary")

    def test_spaces_after_commas_do_not_survive(self):
        """Via irc.configured_channels(), so this cannot drift from the JOIN."""
        self.set_config(CHANNEL=" #one , #two ")

        self.assertEqual(dcc.default_announce_channel(), "#one")

    def test_a_blank_channel_setting_does_not_raise(self):
        """config.CHANNEL is None for part of every rehash, and this runs on
        the transfer path."""
        for blank in ("", None, "  ", ","):
            with self.subTest(blank=blank):
                self.set_config(CHANNEL=blank)

                self.assertEqual(dcc.default_announce_channel(), "")


class AQueueEntryWithoutAChannelStillTargetsOne(DCCoreTestCase):
    """The expression as dcc.py now evaluates it, for each entry shape that
    reaches it."""

    def resolve(self, next_file):
        # dcc.announce_channel_for() itself, not a copy of it in this file. The
        # first version of this test reimplemented the expression and passed
        # happily while the module went back to the old list default - the
        # mutation run is what caught it.
        return dcc.announce_channel_for(next_file)

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#one,#two")

    def test_an_entry_that_names_a_channel_keeps_it(self):
        self.assertEqual(self.resolve({"channel": "#chosen"}), "#chosen")

    def test_an_entry_with_no_channel_key(self):
        target = self.resolve({"file": "Song.flac"})

        self.assertIsInstance(target, str)
        self.assertEqual(target, "#one")

    def test_an_entry_with_an_empty_channel(self):
        """`.get('channel', default)` returned the empty string here, because
        the key was present - so only the missing-key case was ever handled."""
        for empty in ("", None):
            with self.subTest(empty=empty):
                self.assertEqual(self.resolve({"channel": empty}), "#one")

    def test_a_non_dict_entry(self):
        self.assertEqual(self.resolve("legacy-string-entry"), "#one")

    def test_the_wire_line_is_never_a_rendered_list(self):
        """Stated as the server sees it. This is what the defect actually
        produced, and no assertion about types would have shown it."""
        for entry in ({"file": "x"}, {"channel": ""}, "legacy"):
            with self.subTest(entry=entry):
                line = f"PRIVMSG {self.resolve(entry)} :Sent x to nick"

                self.assertNotIn("[", line)
                self.assertNotIn("'", line)
                self.assertTrue(line.startswith("PRIVMSG #"))


class TheAdvertLoopReadsTheChannelsSafely(DCCoreTestCase):
    """announce_worker() is one of only two functions no test enters - it is a
    `while True` that owns its thread - so its body is read out of the source
    instead."""

    def test_the_worker_does_not_split_the_setting_by_hand(self):
        with io.open(os.path.join(REPO_ROOT, "announce.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        body = source.split("def announce_worker(", 1)[1]

        self.assertNotIn("config.CHANNEL.split(", body,
                         "announce_worker() is splitting config.CHANNEL by hand "
                         "again - that is an AttributeError on the advert thread "
                         "while a rehash has CHANNEL set to None")
        self.assertIn("irc.configured_channels()", body)


class TheGlobalScanSeparatesTheTwoQuestions(unittest.TestCase):
    """The correction to what this file said when it was written.

    The first version recorded that the global-queue scan "deliberately" kept
    a list, on the grounds that it was only a membership test - and asserted
    that the type-switch on `g_chan` must stay. It was not only a membership
    test: g_chan is passed to start_dcc_send() at the thread spawn a few lines
    below, which hands it to announce.send_transfer_complete(), which builds
    `f"PRIVMSG {channel} :"`. So the test written to protect that shape was
    protecting the identical defect the rest of this file exists for, and the
    audit is what caught it.

    There are two questions and they need two values:

      "which channels prove this user is present" wants a LIST - the entry may
      name one, and an entry naming none must be checked against every
      configured channel.

      "where do we announce the finished transfer" wants exactly ONE.
    """

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_membership_check_still_handles_a_list(self):
        """Narrowing it to one channel would silently stop the scan finding a
        user who is present in the second of two."""
        source = self.source()

        self.assertIn("isinstance(raw_chan, list)", source,
                      "the global-queue scan no longer handles a list of "
                      "channels for its membership test")

    def test_the_announce_target_comes_from_the_helper(self):
        """And not from the same value the membership test uses."""
        source = self.source()

        self.assertIn("g_chan = announce_channel_for(g_next)", source)

    def test_the_scan_no_longer_defaults_the_target_to_a_split(self):
        source = self.source()

        self.assertNotIn("g_chan = g_next.get('channel', config.CHANNEL.split(','))",
                         source,
                         "the global-queue scan is defaulting its announce "
                         "target to a LIST of channels again")


class ATargetIsAlwaysOneChannel(DCCoreTestCase):
    """announce_channel_for() had the same hole one level down: `if named:`
    accepted a LIST, so it fixed the default and let a stored one through."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#one,#two")

    def test_a_list_stored_in_the_entry_does_not_become_the_target(self):
        target = dcc.announce_channel_for({"channel": ["#one", "#two"]})

        self.assertIsInstance(target, str)
        self.assertEqual(target, "#one")

    def test_a_non_string_channel_falls_back(self):
        for junk in ({}, 12, True, [], ["#x"]):
            with self.subTest(junk=junk):
                target = dcc.announce_channel_for({"channel": junk})

                self.assertIsInstance(target, str)
                self.assertNotIn("[", target)

    def test_a_padded_channel_is_stripped(self):
        """It goes straight into a PRIVMSG, where a space ends the target."""
        self.assertEqual(dcc.announce_channel_for({"channel": "  #chosen  "}),
                         "#chosen")

    def test_the_wire_line_is_never_a_rendered_list(self):
        for entry in ({"channel": ["#one"]}, {"channel": {}}, {"channel": 5},
                      {"file": "x"}, "legacy"):
            with self.subTest(entry=entry):
                line = f"PRIVMSG {dcc.announce_channel_for(entry)} :Sent x"

                self.assertNotIn("[", line)
                self.assertNotIn("{", line)
                self.assertTrue(line.startswith("PRIVMSG #"))


if __name__ == "__main__":
    unittest.main()
