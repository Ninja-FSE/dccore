"""The freshness LED says WHAT it is comparing, not just its verdict.

From a maintainer: "redownloaded [a bot's] list, its yellow, but it does not
update to green."

The tooltip said only "Their list has changed since you downloaded it". That
is the verdict with the evidence removed, and three quite different situations
produce it:

  * the re-download never landed (refused, timed out, still in flight)
  * it landed and was rejected, so the list we hold is still the old one
  * it landed fine, and the bot advertised something newer again afterwards

From outside they look identical, and telling them apart meant reading the
daemon log. The payload has carried `advert_then` and `advert_now` since the
LED was built - the exact two values webserver._freshness() decides on - and
the LED was dropping them.

The first class here is the control: the backend really does move a row to
"current" once a re-fetch rewrites the stored snapshot. So a LED that stays
yellow is reporting something true, and the tooltip is where the answer has
to come from rather than a bug to go fix in the comparison.
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "someotherbot"


def _list_txt(base="SomeOtherBot"):
    return "".join(f"!{base} Track{i}.flac  ::INFO:: 5000000\n" for i in range(4))


def _write_zip(path, arcname):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(arcname, _list_txt())


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                 encoding="utf-8") as handle:
        return handle.read()


def function_body(source, name):
    """The text of one top-level function in app.js. They are all written at
    two-space indentation, so the first closing brace at that indent after the
    signature is the function's own."""
    return source.split("function " + name + "(", 1)[1].split("\n  }", 1)[0]


class ARefetchDoesTurnTheLedGreen(DCCoreTestCase):
    """The control. If this failed, the tooltip would be papering over a real
    bug rather than explaining a real verdict."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-led-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

    def fetch(self, arcname):
        path = os.path.join(self.tmp, arcname.replace(".txt", ".zip"))
        _write_zip(path, arcname)
        ok, reason = list_fetch.process_fetched_list_zip(BOT, path)
        self.assertTrue(ok, reason)

    def freshness(self):
        rows = {row["bot"]: row
                for row in webserver.build_fetched_bot_list_summaries()}
        return rows[BOT]["freshness"]

    def test_green_then_yellow_then_green_again(self):
        runtime.known_bots[BOT] = {"files": 100, "list_date": "2026-08-01"}
        self.fetch("SomeOtherBot-2026-08-01.txt")
        self.assertEqual(self.freshness(), "current")

        runtime.known_bots[BOT] = {"files": 120, "list_date": "2026-09-05"}
        self.assertEqual(self.freshness(), "changed")

        self.fetch("SomeOtherBot-2026-09-05.txt")

        self.assertEqual(self.freshness(), "current")

    def test_the_row_carries_both_sides_of_the_comparison(self):
        """What the tooltip needs. Absent these, the page has the verdict and
        nothing else."""
        runtime.known_bots[BOT] = {"files": 100, "list_date": "2026-08-01"}
        self.fetch("SomeOtherBot-2026-08-01.txt")
        runtime.known_bots[BOT] = {"files": 120, "list_date": "2026-09-05"}

        row = {r["bot"]: r
               for r in webserver.build_fetched_bot_list_summaries()}[BOT]

        self.assertEqual(row["advert_then"],
                         {"files": 100, "list_date": "2026-08-01"})
        self.assertEqual(row["advert_now"],
                         {"files": 120, "list_date": "2026-09-05"})


class TheTooltipShowsThem(unittest.TestCase):

    def body(self):
        return function_body(app_js(), "ledTitle")

    def test_it_is_given_the_whole_row_not_just_the_verdict(self):
        """It cannot show either advert if all it receives is a string."""
        source = app_js()

        self.assertIn("ledTitle(row)", source)
        self.assertNotIn("ledTitle(row.freshness)", source)

    def test_the_changed_tooltip_reports_both_sides(self):
        changed = self.body().split('freshness === "changed"', 1)[1] \
                             .split("if (", 1)[0]

        self.assertIn("advert_then", changed)
        self.assertIn("advert_now", changed)

    def test_it_still_names_the_verdict(self):
        """The evidence is added to the verdict, not swapped for it."""
        self.assertIn("changed since you downloaded it", self.body())

    def test_a_green_led_says_what_it_matched(self):
        """"Current" on its own leaves you unable to tell a genuinely fresh
        list from one whose bot simply stopped advertising."""
        current = self.body().rsplit("return", 1)[1]

        self.assertIn("advert_now", current)

    def test_an_undownloaded_bot_says_what_is_on_offer(self):
        not_held = self.body().split('freshness === "not_held"', 1)[1] \
                              .split("if (", 1)[0]

        self.assertIn("advert_now", not_held)

    def test_the_title_is_set_as_a_property(self):
        """These strings come off another bot's advert, and escapeHtml()
        encodes & < > but leaves a double quote alone - so this must never
        become part of a concatenated attribute."""
        # On the NAME now. The dot became presence when the two signals
        # were split, and the freshness explanation went with the colour it
        # explains. "primary", not "row": #399 grouped a bot's several lists
        # under one sidebar row, whose own signals come from whichever of
        # that group's lists speaks for it (see primaryEntry() in app.js).
        self.assertIn("name.title = ledTitle(primary);", app_js())


class ItReusesTheBannerFormatter(unittest.TestCase):
    """describeAdvert() already existed, for the freshness BANNER that spells
    the same two adverts out for whichever bot is SELECTED in the List
    Browser. A second function of that name would not have been a duplicate so
    much as a silent override - JavaScript hoists both declarations and the
    later one wins - leaving the LED and the banner free to tell different
    stories about one row."""

    def test_there_is_exactly_one_describe_advert(self):
        self.assertEqual(app_js().count("function describeAdvert("), 1)

    def test_the_led_uses_it(self):
        self.assertIn("describeAdvert(", function_body(app_js(), "ledTitle"))

    def test_the_banner_still_uses_it_too(self):
        banner = function_body(app_js(), "renderFilelistsFreshness")

        self.assertIn("describeAdvert(", banner)

    def test_it_is_given_an_object_even_when_the_row_has_none(self):
        """The banner's formatter reads advert.files directly, so a row that
        carries no advert at all must not reach it as undefined."""
        body = function_body(app_js(), "ledTitle")

        self.assertEqual(body.count("|| {}"), body.count("describeAdvert("))


if __name__ == "__main__":
    unittest.main()
