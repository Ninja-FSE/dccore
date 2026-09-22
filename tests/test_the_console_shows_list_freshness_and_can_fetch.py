"""#750: `lists` and `fetch` in the console, and `LISTFETCH` in the feed.

The dashboard's List Browser shows whether a held bot list has changed since we
took our copy, and the automatic refresh asks again; the console had nothing.
`lists` prints the same verdicts; `fetch` asks the changed ones (or one bot)
through the dashboard's own enqueue; a structured client is told with
`DCCORE LISTFETCH <bot> <action> <text>` when an automatic refresh asks a bot,
when a list arrives and when it cannot be used.
"""

import io
import os
import sys
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class FakeSession:
    nick = "web:test"

    def __init__(self):
        self.lines = []

    def send(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


def row(bot, freshness="changed", count=1000, age=3600, online=True, held=True, list_marker=""):
    return {"bot": f"{bot}/{list_marker}" if list_marker else bot, "nick": bot, "list": list_marker,
            "held": held, "freshness": freshness, "count": count, "online": online,
            "fetched_at": time.time() - age}


class WithSummaries(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.rows = []
        self.asked = []
        self.refuse = {}

        def enqueue(nick):
            self.asked.append(nick)
            if nick in self.refuse:
                return self.refuse[nick]
            return 200, {"created": [1]}

        for target, value in (("build_fetched_bot_list_summaries", lambda: list(self.rows)),
                              ("build_list_fetch_enqueue_result", enqueue)):
            patch = mock.patch.object(webserver, target, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.session = FakeSession()


class TheLists(WithSummaries):

    def test_it_says_so_when_nothing_is_held(self):
        adminchat._cmd_lists(self.session, "")
        self.assertIn("No bot's list is held", self.session.text)

    def test_it_counts_and_names_the_changed_ones_first(self):
        self.rows = [row("Alpha", "current"), row("Bravo", "changed", 64136, age=7200),
                     row("Charlie", "unknown", online=None)]
        adminchat._cmd_lists(self.session, "")
        lines = self.session.lines
        self.assertEqual(lines[0], "3 held list(s), 1 changed since we took our copy:")
        self.assertIn("Bravo", lines[1])
        self.assertIn("changed", lines[1])
        self.assertIn("64,136 files", lines[1])
        self.assertIn("fetched 2 h ago", lines[1])
        self.assertIn("online", lines[1])
        self.assertTrue(any("Alpha" in l and "current" in l for l in lines))
        self.assertTrue(any("Charlie" in l and "unknown" in l for l in lines))

    def test_it_points_at_fetch_only_when_something_changed(self):
        self.rows = [row("Alpha", "current")]
        adminchat._cmd_lists(self.session, "")
        self.assertNotIn("Ask for the changed ones", self.session.text)
        self.rows = [row("Alpha", "changed")]
        self.session = FakeSession()
        adminchat._cmd_lists(self.session, "")
        self.assertIn("`fetch`", self.session.text)

    def test_offline_is_shown(self):
        self.rows = [row("Alpha", "changed", online=False)]
        adminchat._cmd_lists(self.session, "")
        self.assertIn("offline", self.session.text)

    def test_a_bot_with_several_lists_is_one_line(self):
        self.rows = [row("Alpha", "changed", 500), row("Alpha", "changed", 40, list_marker="films")]
        adminchat._cmd_lists(self.session, "")
        self.assertEqual(self.session.lines[0], "1 held list(s), 1 changed since we took our copy:")
        self.assertEqual(len([l for l in self.session.lines if "Alpha" in l]), 1)

    def test_a_bot_only_seen_advertising_is_not_a_held_list(self):
        self.rows = [row("Seen", "not_held", held=False), row("Alpha", "current")]
        adminchat._cmd_lists(self.session, "")
        self.assertNotIn("Seen", self.session.text)

    def test_ages_read_in_the_right_unit(self):
        self.assertEqual(adminchat._age_text(time.time() - 30), "30 s")
        self.assertEqual(adminchat._age_text(time.time() - 600), "10 min")
        self.assertEqual(adminchat._age_text(time.time() - 3 * 3600), "3 h")
        self.assertEqual(adminchat._age_text(time.time() - 5 * 86400), "5 d")
        self.assertEqual(adminchat._age_text("never"), "?")

    def test_it_is_a_registered_command(self):
        self.assertIn("lists", adminchat.COMMANDS)


class TheFetch(WithSummaries):

    def test_it_asks_only_the_changed_ones(self):
        self.rows = [row("Alpha", "current"), row("Bravo", "changed"), row("Charlie", "unknown"),
                     row("Delta", "changed")]
        adminchat._cmd_fetch(self.session, "")
        self.assertEqual(sorted(self.asked), ["Bravo", "Delta"])
        self.assertIn("Asked 2 bot(s)", self.session.text)

    def test_the_oldest_fetch_goes_first(self):
        self.rows = [row("New", "changed", age=100), row("Old", "changed", age=99999)]
        adminchat._cmd_fetch(self.session, "")
        self.assertEqual(self.asked, ["Old", "New"])

    def test_an_offline_bot_is_not_asked(self):
        self.rows = [row("Gone", "changed", online=False), row("Here", "changed", online=True),
                     row("Unsure", "changed", online=None)]
        adminchat._cmd_fetch(self.session, "")
        self.assertEqual(sorted(self.asked), ["Here", "Unsure"])
        self.assertIn("Gone (offline)", self.session.text)

    def test_nothing_changed_says_so_and_asks_nobody(self):
        self.rows = [row("Alpha", "current"), row("Bravo", "unknown")]
        adminchat._cmd_fetch(self.session, "")
        self.assertEqual(self.asked, [])
        self.assertIn("Nothing to fetch", self.session.text)

    def test_one_command_asks_at_most_ten(self):
        self.rows = [row(f"Bot{n:02d}", "changed", age=1000 + n) for n in range(14)]
        adminchat._cmd_fetch(self.session, "")
        self.assertEqual(len(self.asked), adminchat.FETCH_COMMAND_MAX)
        self.assertEqual(adminchat.FETCH_COMMAND_MAX, 10)
        self.assertIn("over the limit of 10", self.session.text)
        self.assertEqual(self.session.text.count("over the limit"), 4)

    def test_a_refusal_is_reported_and_the_rest_go_on(self):
        self.rows = [row("Busy", "changed", age=9000), row("Fine", "changed", age=10)]
        self.refuse["Busy"] = (409, {"error": "a fetch for that bot is already outstanding"})
        adminchat._cmd_fetch(self.session, "")
        self.assertEqual(self.asked, ["Busy", "Fine"])
        self.assertIn("Busy: a fetch for that bot is already outstanding", self.session.text)
        self.assertIn("Asked 1 bot(s)", self.session.text)

    def test_one_bot_is_asked_whatever_its_freshness(self):
        self.rows = [row("Alpha", "current")]
        adminchat._cmd_fetch(self.session, "Alpha")
        self.assertEqual(self.asked, ["Alpha"])
        self.assertIn("asked Alpha for its list", self.session.text)

    def test_one_bot_that_is_refused_says_why(self):
        self.refuse["Nope"] = (400, {"error": "'bot' has a space"})
        adminchat._cmd_fetch(self.session, "Nope")
        self.assertIn("Nope: 'bot' has a space", self.session.text)

    def test_it_uses_the_dashboards_own_enqueue(self):
        with open(os.path.join(REPO_ROOT, "adminchat.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("webserver.build_list_fetch_enqueue_result(nick)", source)

    def test_it_is_a_registered_command(self):
        self.assertIn("fetch", adminchat.COMMANDS)


class TheFeedLine(unittest.TestCase):

    def test_the_line_is_bot_action_text(self):
        line = adminchat.structured_line("LISTFETCH", {"bot": "Alpha", "action": "arrived",
                                                       "text": "Alpha's list arrived: 64,136 files"})
        self.assertEqual(line, "DCCORE LISTFETCH Alpha arrived Alpha's list arrived: 64,136 files")

    def test_the_bot_and_action_stay_one_token_each(self):
        line = adminchat.structured_line("LISTFETCH", {"bot": "a b", "action": "x y", "text": "t"})
        self.assertTrue(line.startswith("DCCORE LISTFETCH a_b x_y "))

    def test_it_is_a_feed_kind_so_a_structured_session_takes_it_as_fields(self):
        self.assertIn("LISTFETCH", adminchat.FEED_KINDS)

    def test_it_reaches_the_debug_channel_only_with_the_feed_on(self):
        self.assertIn("LISTFETCH", announce.FEED_ONLY_CATEGORIES)

    def test_it_has_a_tag(self):
        import theme
        label, _colour = announce.category_tag("LISTFETCH", theme.blocks())
        self.assertEqual(label, "LISTS")


class TheEvents(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.events = []
        real = announce.feed_event
        announce.feed_event = lambda kind, text, **fields: self.events.append((kind, text, fields))
        self.addCleanup(setattr, announce, "feed_event", real)

    def test_an_automatic_ask_is_told(self):
        list_fetch._tell_the_console("Alpha", "auto", "Alpha's list has changed - asking again automatically")
        self.assertEqual(self.events, [("LISTFETCH", "Alpha's list has changed - asking again automatically",
                                        {"bot": "Alpha", "action": "auto"})])

    def test_the_sweep_tells_the_console_for_each_bot_it_asks(self):
        self.set_config(bot_joined_channel=True)
        with mock.patch.object(list_fetch, "lists_worth_refetching", lambda now=None: ["Alpha", "Bravo"]), \
                mock.patch.object(webserver, "build_list_fetch_enqueue_result", lambda bot: (200, {})), \
                mock.patch.object(list_fetch, "_note_auto_attempt", lambda bot, when: None):
            started = list_fetch.refetch_due_lists(log=lambda *_: None, now=1000.0)
        self.assertEqual(started, ["Alpha", "Bravo"])
        self.assertEqual([(k, f["bot"], f["action"]) for k, _t, f in self.events],
                         [("LISTFETCH", "Alpha", "auto"), ("LISTFETCH", "Bravo", "auto")])

    def test_a_refused_ask_is_not_announced_as_asked(self):
        self.set_config(bot_joined_channel=True)
        with mock.patch.object(list_fetch, "lists_worth_refetching", lambda now=None: ["Alpha"]), \
                mock.patch.object(webserver, "build_list_fetch_enqueue_result", lambda bot: (409, {"error": "busy"})):
            self.assertEqual(list_fetch.refetch_due_lists(log=lambda *_: None, now=1000.0), [])
        self.assertEqual(self.events, [])

    def test_an_arrived_list_says_how_many_files(self):
        config.fetched_bot_lists["alpha"] = {"bot": "Alpha", "entry_count": 64136}
        self.addCleanup(config.fetched_bot_lists.pop, "alpha", None)
        with mock.patch.object(list_fetch, "_process_fetched_list_zip_unlocked", lambda bot, path: (True, "")):
            self.assertEqual(list_fetch.process_fetched_list_zip("Alpha", "x.zip"), (True, ""))
        self.assertEqual(self.events[0][2], {"bot": "Alpha", "action": "arrived"})
        self.assertIn("64,136 files", self.events[0][1])

    def test_an_unusable_list_says_why(self):
        with mock.patch.object(list_fetch, "_process_fetched_list_zip_unlocked",
                               lambda bot, path: (False, "not a plausible list")):
            self.assertEqual(list_fetch.process_fetched_list_zip("Alpha", "x.zip"), (False, "not a plausible list"))
        self.assertEqual(self.events[0][2], {"bot": "Alpha", "action": "unusable"})
        self.assertIn("not a plausible list", self.events[0][1])

    def test_the_return_value_is_untouched(self):
        with mock.patch.object(list_fetch, "_process_fetched_list_zip_unlocked", lambda bot, path: (False, "r")):
            self.assertEqual(list_fetch.process_fetched_list_zip("A", "z"), (False, "r"))

    def test_a_console_that_raises_does_not_fail_the_fetch(self):
        announce.feed_event = mock.Mock(side_effect=RuntimeError("no console"))
        with mock.patch("builtins.print"):
            list_fetch._tell_the_console("Alpha", "auto", "t")
        with mock.patch.object(list_fetch, "_process_fetched_list_zip_unlocked", lambda bot, path: (True, "")), \
                mock.patch("builtins.print"):
            self.assertEqual(list_fetch.process_fetched_list_zip("Alpha", "x.zip"), (True, ""))


class TheWindow(unittest.TestCase):

    def script(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
            return handle.read().replace("\r\n", "\n")

    def test_the_two_commands_go_to_the_bot(self):
        text = self.script()
        self.assertIn("if (%cmd == lists) { dccore.send lists | return }", text)
        self.assertIn("if (%cmd == fetch) { dccore.send fetch $2- | return }", text)

    def test_the_new_line_prints_the_text_with_its_own_tag(self):
        text = self.script()
        block = text[text.index("if (%type == LISTFETCH) {"):]
        block = block[:block.index("\n  }")]
        self.assertIn("dccore.echo $dccore.tag(LISTS,search) $4-", block)

    def test_the_help_lists_them(self):
        text = self.script()
        self.assertIn("/dccore lists", text)
        self.assertIn("/dccore fetch [bot]", text)

    def test_the_positions_are_the_ones_the_bot_writes(self):
        """`$2` bot, `$3` action, `$4-` text - after the type."""
        line = adminchat.structured_line("LISTFETCH", {"bot": "B", "action": "auto", "text": "one two"})
        fields = line.split(" ")
        self.assertEqual(fields[:4], ["DCCORE", "LISTFETCH", "B", "auto"])
        self.assertEqual(" ".join(fields[4:]), "one two")


if __name__ == "__main__":
    unittest.main()
