"""#133's source list: one row per bot, with a dot for the state of its list.

WHY IT REPLACED A <select>

A dropdown can hold a nick and a count and nothing else. #133 asks for the
state of each list to be visible without opening anything, and for bots we
hold *no* list from to appear as well - "not downloaded" is one of the three
colours it names, and a dropdown of things you can switch to has nowhere to
put a thing you cannot.

So the rows come from two places: the lists we hold, and the adverts we have
seen. A bot in both is one row, the held one - it has a real freshness verdict
and can actually be browsed.

WHAT THE COLOUR IS NEVER ALLOWED TO DO

Claim more than we know. A freshness the page does not recognise renders grey
(cannot tell), never green - and clicking a bot whose list we do not hold must
not switch the table to a source that would come back empty.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def web_file(name):
    with io.open(os.path.join(REPO_ROOT, "web", name), encoding="utf-8") as fh:
        return fh.read()


class TheRowsComeFromBothPlaces(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        original = dict(runtime.known_bots)
        runtime.known_bots.clear()
        self.addCleanup(lambda: (runtime.known_bots.clear(),
                                 runtime.known_bots.update(original)))

    def rows(self):
        return {row["bot"]: row
                for row in webserver.build_fetched_bot_list_summaries()}

    def test_a_bot_we_hold_a_list_from_is_held(self):
        runtime.known_bots["reelbot"] = {
            "nick": "ReelBot", "files": 12004, "list_date": "Aug 28th"}
        self.set_config(fetched_bot_lists={
            "reelbot": {"bot": "ReelBot", "fetched_at": 1,
                          "entry_count": 12004,
                          "advert_when_fetched": {"files": 12004,
                                                  "list_date": "Aug 28th"}}})

        row = self.rows()["ReelBot"]

        self.assertIs(row["held"], True)
        self.assertEqual(row["freshness"], "current")

    def test_a_bot_we_have_only_seen_advertising_is_a_row_too(self):
        """The whole reason the dropdown had to go: this bot has no list to
        switch to, and the operator still needs to see it is there."""
        runtime.known_bots["metalhead"] = {
            "nick": "MetalHead", "files": 44000, "list_date": "Sep 1st"}
        self.set_config(fetched_bot_lists={})

        row = self.rows()["MetalHead"]

        self.assertIs(row["held"], False)
        self.assertEqual(row["freshness"], "not_held")

    def test_a_bot_that_is_both_appears_once_as_the_held_one(self):
        """A held row carries a real verdict and a real count we parsed. The
        advert row would overwrite both with a claim and a nothing."""
        runtime.known_bots["boombox"] = {
            "nick": "BoomBox", "files": 8110, "list_date": "Aug 28th"}
        self.set_config(fetched_bot_lists={
            "boombox": {"bot": "BoomBox", "fetched_at": 99,
                         "entry_count": 7902,
                         "advert_when_fetched": {"files": 7902,
                                                 "list_date": "Aug 10th"}}})

        rows = [row for row in webserver.build_fetched_bot_list_summaries()
                if row["bot"] == "BoomBox"]

        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["held"], True)
        self.assertEqual(rows[0]["count"], 7902)
        self.assertEqual(rows[0]["freshness"], "changed")

    def test_the_count_on_a_not_held_row_is_what_they_advertise(self):
        runtime.known_bots["metalhead"] = {"nick": "MetalHead", "files": 44000}
        self.set_config(fetched_bot_lists={})

        self.assertEqual(self.rows()["MetalHead"]["count"], 44000)

    def test_a_bot_that_published_no_count_reports_none_not_zero(self):
        """Zero is a number they never said. The page renders absent as a
        dash; it would render zero as a count, which is a claim."""
        runtime.known_bots["quietbot"] = {"nick": "QuietBot",
                                          "list_date": "Sep 1st"}
        self.set_config(fetched_bot_lists={})

        self.assertIsNone(self.rows()["QuietBot"]["count"])

    def test_the_display_nick_is_the_one_they_use_not_the_lookup_key(self):
        """known_bots is keyed lower-case; the row is read by a person."""
        runtime.known_bots["metalhead"] = {"nick": "MetalHead", "files": 1}
        self.set_config(fetched_bot_lists={})

        self.assertIn("MetalHead", self.rows())

    def test_both_kinds_sort_together_case_insensitively(self):
        """Not held-first-then-seen: the operator is looking for a nick, and
        which pile it came from is not something they know in advance."""
        runtime.known_bots.update({
            "apex": {"nick": "apex", "files": 1},
            "zulu": {"nick": "Zulu", "files": 1},
        })
        self.set_config(fetched_bot_lists={
            "mango": {"bot": "Mango", "fetched_at": 1, "entry_count": 1}})

        order = [row["bot"]
                 for row in webserver.build_fetched_bot_list_summaries()]

        self.assertEqual(order, ["apex", "Mango", "Zulu"])


class AMalformedRegistryEntry(DCCoreTestCase):
    """`data/known_bots.json` is a plain JSON file an operator can open.

    Found the way these things are found: a test in another file seeds every
    shared container with `{"probe": 1}` to prove the containers survive a
    reload, and once this view started reading the registry that probe took
    the route down with a 500 for the rest of the run. The probe was a test
    artefact; the hand-edited file it stands in for is not.

    Nothing here is a security boundary - the file is as trusted as the
    daemon. It is the loader's own promise, written before this view existed:
    a registry that will not parse "costs an empty sidebar until then and
    nothing else". One bad line must cost its own row.
    """

    def setUp(self):
        super().setUp()
        original = dict(runtime.known_bots)
        runtime.known_bots.clear()
        self.addCleanup(lambda: (runtime.known_bots.clear(),
                                 runtime.known_bots.update(original)))

    def test_it_costs_its_own_row_and_no_other(self):
        runtime.known_bots.update({
            "brokenbot": 5,
            "goodbot": {"nick": "GoodBot", "files": 12},
        })
        self.set_config(fetched_bot_lists={})

        rows = webserver.build_fetched_bot_list_summaries()

        self.assertEqual([row["bot"] for row in rows], ["GoodBot"])

    def test_a_list_we_hold_still_renders_beside_one(self):
        """The held rows read the registry as well, through _advert_now().
        They must not go down with it."""
        runtime.known_bots["brokenbot"] = 5
        self.set_config(fetched_bot_lists={
            "goodbot": {"bot": "GoodBot", "fetched_at": 1, "entry_count": 12}})

        rows = webserver.build_fetched_bot_list_summaries()

        self.assertEqual([row["bot"] for row in rows], ["GoodBot"])
        self.assertEqual(rows[0]["freshness"], "unknown")

    def test_a_bots_own_malformed_entry_leaves_it_merely_unknown(self):
        """Not "current" - we read nothing, so we know nothing."""
        runtime.known_bots["goodbot"] = 5
        self.set_config(fetched_bot_lists={
            "goodbot": {"bot": "GoodBot", "fetched_at": 1, "entry_count": 12,
                        "advert_when_fetched": {"files": 12,
                                                "list_date": "Aug 1st"}}})

        self.assertEqual(
            webserver.build_fetched_bot_list_summaries()[0]["freshness"],
            "unknown")


class TheDotNeverClaimsMoreThanWeKnow(unittest.TestCase):
    """Read out of app.js: nothing in this project executes JavaScript."""

    @classmethod
    def setUpClass(cls):
        cls.js = web_file("app.js")

    def block(self, name):
        start = self.js.index("function " + name + "(")
        depth = 0
        for i in range(self.js.index("{", start), len(self.js)):
            if self.js[i] == "{":
                depth += 1
            elif self.js[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.js[start:i + 1]
        raise AssertionError("unbalanced braces in " + name)

    def test_an_unrecognised_freshness_falls_to_cannot_tell(self):
        """The default arm, not a green one. A verdict the page has never
        heard of - added later, or a garbled record - must not be painted as
        current; that is the one colour telling the operator this list needs
        no more thought.

        On the NAME now rather than the dot. The dot was carrying freshness,
        which left presence shown nowhere, so the two were split."""
        body = self.block("freshnessClass")
        last_return = body.rstrip().rstrip("}").rstrip().splitlines()[-1]

        self.assertIn("is-unknown", last_return)

    def test_every_state_the_server_can_send_has_its_own_colour(self):
        """The four the server emits, plus "own" for our own list. A state
        with no arm here would silently render grey."""
        body = self.block("freshnessClass")

        for state in ("current", "changed", "not_held", "own"):
            self.assertIn('"' + state + '"', body,
                          state + " has no arm in freshnessClass()")

    def test_presence_has_a_third_state_of_its_own(self):
        """`null` is not "offline". The membership mirror is empty while the
        bot is still joining, and every nick would read as gone."""
        body = self.block("presenceClass")

        self.assertIn("is-online", body)
        self.assertIn("is-offline", body)
        self.assertIn("is-presence-unknown", body)

    def test_both_signals_also_say_it_in_words(self):
        """Colour alone is invisible to roughly one man in twelve, so the dot
        and the name each carry a title of their own."""
        row = self.block("botRow")

        self.assertIn("led.title = presenceTitle(", row)
        self.assertIn("name.title = ledTitle(", row)

        titles = self.block("ledTitle")
        for state in ("changed", "not_held", "unknown"):
            self.assertIn('"' + state + '"', titles)

        self.assertIn("channel", self.block("presenceTitle"))


class ClickingARowYouCannotBrowse(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.js = web_file("app.js")
        start = cls.js.index('el.filelistsBotList.addEventListener("click"')
        cls.handler = cls.js[start:cls.js.index("\n  });", start)]

    def not_held_arm(self):
        """The body of `if (row.dataset.held === "no") { ... }`, brace-matched.

        Sliced this narrowly on purpose. A span running from the check down to
        the source switch also contains the "already showing this one" early
        return, so deleting the arm's own return left such a span still
        holding the word - the test passed against exactly the code it exists
        to reject.
        """
        start = self.handler.index('if (row.dataset.held === "no") {')
        depth = 0
        for i in range(self.handler.index("{", start), len(self.handler)):
            if self.handler[i] == "{":
                depth += 1
            elif self.handler[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.handler[start:i + 1]
        raise AssertionError("unbalanced braces in the not-held arm")

    def test_a_not_held_row_does_not_become_the_source(self):
        """There is no list behind it. Switching would page an empty table and
        leave the operator wondering what they broke."""
        arm = self.not_held_arm()

        self.assertIn("return;", arm,
                      "the not-held arm falls through into the source switch")
        self.assertNotIn("state.filelistsSource =", arm,
                         "the not-held arm sets the source itself")

    def test_it_puts_the_nick_where_fetching_one_starts(self):
        """The NICK, which is no longer the same as the row's identity. A
        bot's other lists are keyed "<nick>/<marker>", and pasting that into
        the fetch box would address a bot that does not exist."""
        self.assertIn("el.filelistsFetchInput.value = row.dataset.nick",
                      self.not_held_arm())

    def test_and_says_why_nothing_else_happened(self):
        """A click that changes nothing visible reads as a broken page."""
        self.assertIn("showFilelistsFetchStatus(", self.not_held_arm())

    def test_a_held_row_resets_the_pager(self):
        """Offset 40 of the previous bot's list is not a page of this one."""
        self.assertIn("state.filelistsOffset = 0", self.handler)
        self.assertIn("state.filelistsHistory = []", self.handler)


class ASourceThatIsNoLongerThere(unittest.TestCase):

    def test_the_remembered_source_must_still_be_one_we_hold(self):
        """Every advertising bot is now in the rows, so "is it in the list"
        stopped being the question - a deleted list leaves its bot present as
        a red row, and the view would sit on a source it cannot page."""
        js = web_file("app.js")
        start = js.index("var stillThere")
        clause = js[start:js.index(";", start)]

        self.assertIn("row.held", clause)


class TheLegendTheDotsAndTheStylesheetAgree(unittest.TestCase):
    """Three files that have to name the same four classes, and no test would
    notice a rename in any one of them alone."""

    @classmethod
    def setUpClass(cls):
        cls.js = web_file("app.js")
        cls.html = web_file("index.html")
        cls.css = web_file("style.css")

    def js_classes(self, function_name):
        start = self.js.index("function " + function_name + "(")
        body = self.js[start:self.js.index("\n  }", start)]
        return set(re.findall(r'"(is-[a-z-]+)"', body))

    def legend(self):
        """BOTH legends. There are two signals now - the dot for presence and
        the name colour for what we hold - so a reader that took only the
        first would report the second as missing."""
        out = []
        at = self.html.find('class="bot-legend"')
        while at != -1:
            out.append(self.html[at:self.html.index("</p>", at)])
            at = self.html.find('class="bot-legend"', at + 1)
        return "\n".join(out)

    def test_the_legend_shows_every_dot_the_page_can_render(self):
        for name in self.js_classes("presenceClass"):
            self.assertIn("led " + name, self.legend(),
                          name + " can be rendered but is not in the legend")

    def test_the_legend_shows_every_name_colour_too(self):
        for name in self.js_classes("freshnessClass"):
            self.assertIn("legend-name " + name, self.legend(),
                          name + " can be rendered but is not in the legend")

    def test_the_legend_shows_nothing_the_page_cannot_render(self):
        renderable = (self.js_classes("presenceClass")
                      | self.js_classes("freshnessClass"))
        shown = set(re.findall(r"(?:led|legend-name) (is-[a-z-]+)",
                               self.legend()))

        self.assertTrue(shown, "the legend reader found nothing at all")
        for name in shown:
            self.assertIn(name, renderable,
                          name + " is in the legend but nothing renders it")

    def test_every_signal_has_a_colour(self):
        """A class with no rule is an invisible mark, and the row then reads
        as having no state at all rather than as one we cannot show."""
        for name in self.js_classes("presenceClass"):
            self.assertIn(".led." + name, self.css,
                          name + " has no dot rule in style.css")
        for name in self.js_classes("freshnessClass"):
            self.assertIn(".bot-row-name." + name, self.css,
                          name + " has no name rule in style.css")

    def test_the_colours_are_palette_tokens_not_literals(self):
        """Both themes are defined token-level; a literal hex would be one
        theme's colour showing through on the other's ground."""
        for line in self.css.splitlines():
            if line.startswith(".led.is-") or line.startswith(".bot-row-name.is-"):
                self.assertIn("var(--", line, line.strip())


class ANumberTooLargeToCarryIsNotRepeated(TheRowsComeFromBothPlaces):
    """`count` for a bot we have not fetched from is THEIR advert text, parsed
    with irc._as_int(), which builds a Python int of any size at all."""

    def test_an_absurd_advertised_count_reads_as_did_not_say(self):
        """JSON has no size limit and JavaScript does: JSON.parse() turns
        anything past 2**53 into the nearest float before the page sees it, so
        a bot advertising twenty-three digits had a DIFFERENT twenty-three
        digit number rendered beside its nick, in thousands separators,
        looking exact."""
        runtime.known_bots["loud"] = {"nick": "Loud", "files": 10 ** 23}

        self.set_config(fetched_bot_lists={})

        row = self.rows()["Loud"]

        self.assertIsNone(row["count"])
        self.assertNotIn("files", row["advert_now"])

    def test_a_real_count_is_untouched(self):
        """The largest lists seen advertising are a few million rows; nothing
        legitimate is anywhere near the ceiling."""
        runtime.known_bots["big"] = {"nick": "Big", "files": 7902000}

        self.set_config(fetched_bot_lists={})

        self.assertEqual(self.rows()["Big"]["count"], 7902000)

    def test_the_boundary_itself_is_carried(self):
        runtime.known_bots["edge"] = {"nick": "Edge", "files": 2 ** 53 - 1}

        self.set_config(fetched_bot_lists={})

        self.assertEqual(self.rows()["Edge"]["count"], 2 ** 53 - 1)


if __name__ == "__main__":
    unittest.main()
