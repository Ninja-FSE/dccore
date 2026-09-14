"""A bot that is not in the channel is not asked, and the page says which.

FROM THE BETA. A list was requested from a nick that was not on the network -
the operator's own WHOIS answered "No such nick" - and the fetch sat in the
queue holding a slot until it timed out, then reported "no response". Which
was true, and useless: nobody was there to respond, and that was knowable
before a line went out.

The daemon already knew. config.channel_users is synced from 353/JOIN/PART for
every channel it is in, and dcc.py has read it as proof of presence before
dispatching a send since long before this. A request is a PRIVMSG into a
channel: a nick that is not in one of ours cannot see it, so refusing is not a
guess about whether they would answer - it is the observation that they were
never asked.

AND THE SIDEBAR NOW SHOWS BOTH THINGS. The dot carried the list's freshness,
which left presence - the thing that decides whether asking is worth anything
- shown nowhere at all. A list can be perfectly current from a bot that signed
off an hour ago. So the dot is whether they are HERE, and the name's colour is
what we hold from them:

    dot     green   in a channel with us      name   green   list current
            red     not in one                       orange  theirs changed
            grey    still joining                    red     not downloaded
                                                     grey    cannot tell

NOTHING KNOWN IS NOT THE SAME AS NOBODY THERE. While the bot is still joining
the membership mirror is empty and every nick would read as gone - so that
state is its own, and it never refuses a fetch.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

CHANNEL = "#somechannel"


class RefusingToAskSomebodyWhoIsNotThere(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=CHANNEL, MAX_FETCH_SLOTS=3)
        config.channel_users[CHANNEL] = {"someotherbot", "someuser"}

    def test_a_list_request_to_an_absent_bot_is_refused(self):
        """The reported case: the fetch was accepted, held a slot, and failed
        a minute later with "no response"."""
        status, result = webserver.build_list_fetch_enqueue_result("nosuchbot")

        self.assertEqual(status, 409)
        self.assertIn("not in any channel", result["error"])

    def test_the_refusal_names_the_bot(self):
        """An error that does not say who it is about is one more thing to
        work out."""
        _status, result = webserver.build_list_fetch_enqueue_result("nosuchbot")

        self.assertIn("nosuchbot", result["error"])

    def test_nothing_is_queued(self):
        webserver.build_list_fetch_enqueue_result("nosuchbot")

        self.assertEqual(config.fetch_queue, {})

    def test_a_bot_that_is_here_is_asked(self):
        """The control. Every assertion above passes just as happily against
        a build that refuses everything."""
        status, result = webserver.build_list_fetch_enqueue_result("someotherbot")

        self.assertEqual(status, 200)
        self.assertTrue(result["created"])

    def test_the_check_ignores_case(self):
        """IRC nicks are case-insensitive, and the mirror is filled from what
        the server sent rather than from what the operator typed."""
        status, _result = webserver.build_list_fetch_enqueue_result("SomeOtherBot")

        self.assertEqual(status, 200)

    def test_a_folder_request_is_refused_too(self):
        status, result = webserver.build_folder_rar_fetch_enqueue_result(
            "nosuchbot", "D:\\Music\\Album")

        self.assertEqual(status, 409)
        self.assertIn("not in any channel", result["error"])

    def test_a_file_request_is_refused_per_item(self):
        """A bulk paste is routinely several bots at once, and one of them
        having signed off is no reason to refuse the rest."""
        status, result = webserver.build_fetch_enqueue_result([
            {"bot": "nosuchbot", "filename": "Gone.flac"},
            {"bot": "someotherbot", "filename": "Here.flac"},
        ])

        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("not in any channel", result["errors"][0]["error"])


class BeforeWeKnowWhoIsHere(DCCoreTestCase):
    """An empty membership mirror is a bot that has not finished joining. Every
    nick would read as absent, and refusing every fetch on that would be worse
    than the timeout it replaces."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=CHANNEL, MAX_FETCH_SLOTS=3)
        config.channel_users.clear()

    def test_nothing_is_refused(self):
        status, _result = webserver.build_list_fetch_enqueue_result("anybot")

        self.assertEqual(status, 200)

    def test_and_no_error_is_invented(self):
        self.assertIsNone(webserver.bot_not_here_error("anybot"))

    def test_a_channel_with_nobody_in_it_is_still_nothing_known(self):
        """Joined, but the NAMES reply has not arrived."""
        config.channel_users[CHANNEL] = set()

        self.assertIsNone(webserver.bot_not_here_error("anybot"))


class EachSignalIsWiredToItsOwnQuestion(unittest.TestCase):
    """The two helpers can both be perfect and still be plugged in the wrong
    way round - which is what a mutation run showed: swapping the dot back to
    freshness, and stripping the name's colour, both passed every other test
    here. So the wiring itself is asserted, not just the pieces."""

    def row(self):
        with open(os.path.join(REPO_ROOT, "web", "app.js"),
                  encoding="utf-8") as handle:
            body = handle.read().split("function botRow(", 1)[1]
        return body.split("\n  }", 1)[0]

    def test_the_dot_is_fed_by_presence(self):
        # "primary", not "row": #399 grouped a bot's several lists under one
        # sidebar row, and the row's own signals now come from whichever of
        # that group's lists speaks for the collapsed row (see primaryEntry()
        # in app.js) - the wiring under test is unchanged, only its name.
        self.assertIn('led.className = "led " + presenceClass(primary.online);',
                      self.row())

    def test_the_name_is_fed_by_freshness(self):
        self.assertIn(
            'name.className = "bot-row-name " + freshnessClass(primary.freshness);',
            self.row())

    def test_neither_reads_the_other_s_field(self):
        """The failure this catches is not a missing call - it is the right
        call with the wrong argument."""
        row = self.row()

        self.assertNotIn("presenceClass(primary.freshness)", row)
        self.assertNotIn("freshnessClass(primary.online)", row)


class ThePresenceTheSidebarShows(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=CHANNEL)
        config.channel_users[CHANNEL] = {"someotherbot"}
        config.fetched_bot_lists["someotherbot"] = {
            "bot": "someotherbot", "fetched_at": 1, "list_path": "x.txt",
            "entry_count": 5, "advert_when_fetched": {},
        }
        config.fetched_bot_lists["goneaway"] = {
            "bot": "goneaway", "fetched_at": 1, "list_path": "y.txt",
            "entry_count": 5, "advert_when_fetched": {},
        }

    def rows(self):
        return {row["bot"]: row
                for row in webserver.build_fetched_bot_list_summaries()}

    def test_a_bot_in_the_channel_is_online(self):
        self.assertIs(self.rows()["someotherbot"]["online"], True)

    def test_a_bot_we_hold_a_list_from_but_cannot_see_is_offline(self):
        """The case the dot could never show while it carried freshness: a
        current list from a bot that has signed off."""
        self.assertIs(self.rows()["goneaway"]["online"], False)

    def test_presence_and_freshness_are_separate_answers(self):
        rows = self.rows()

        self.assertEqual(rows["someotherbot"]["freshness"],
                         rows["goneaway"]["freshness"])
        self.assertNotEqual(rows["someotherbot"]["online"],
                            rows["goneaway"]["online"])

    def test_it_is_unknown_rather_than_offline_before_names_arrives(self):
        config.channel_users.clear()

        self.assertIsNone(self.rows()["someotherbot"]["online"])

    def test_our_own_lists_are_always_here(self):
        own = [row for row in webserver.build_own_list_summaries()]

        self.assertTrue(all(row["online"] is True for row in own))

    def test_the_membership_is_read_once_per_payload(self):
        """This route is polled every few seconds and a busy channel has
        dozens of advertisers; asking per row would rescan every channel's
        membership per row, per poll."""
        source = open(os.path.join(REPO_ROOT, "webserver.py"),
                      encoding="utf-8").read()
        body = source.split("def build_fetched_bot_list_summaries(", 1)[1]
        body = body.split("\ndef ", 1)[0]
        # Comments stripped: the reason this is built once is written down
        # beside it, and it names the per-row call it exists to avoid.
        code = "\n".join(line for line in body.splitlines()
                         if not line.lstrip().startswith("#"))

        self.assertEqual(code.count("present_nicks()"), 1)
        self.assertNotIn("user_is_present_in_ram", code)


if __name__ == "__main__":
    unittest.main()
