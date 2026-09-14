"""Every list this bot serves can be found in the List Browser.

FOUND IN A BETA. An operator built a second list through the dashboard, put
every film in it, and then could not find it:

    "in list browser i dont see all my lists"
    "only music list"
    "video list isnt there"

The list was being served correctly and advertised correctly in its own
channel. Only the page that browses lists stopped at the primary, and for two
independent reasons:

  * build_filelists_payload() called find_matching_entries() with `name=None`,
    which resolves to the PRIMARY served list. Everything under another
    list's own directory was never opened.
  * the sidebar hard-coded one row - `{bot: "__own__", label: "Our own list"}`
    - so there was nowhere to click even once the backend could answer.

Multi-list is this release's headline feature, and the dashboard is where the
operator created the list. A page that offers to make a thing and then will
not show it is a round trip that does not close.

WHAT DID NOT CHANGE, deliberately: "__own__" alone still means the primary,
and GET /api/filelists with no ?list= still returns it. Every install serving
one list sees exactly what it saw before, down to the row still reading "Our
own list" rather than the name "Main" that nobody chose.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import library  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                 encoding="utf-8") as handle:
        return handle.read()


class TwoServedLists(DCCoreTestCase):
    """The reported shape: a primary whose own list is EMPTY, and a second
    list holding everything. That combination is what made the symptom read as
    "the browser shows nothing of mine" rather than "one list is missing"."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-servedlists-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.lists_dir = os.path.join(self.tmp, "lists")
        os.makedirs(os.path.join(self.lists_dir, "movies-tv"), exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=self.lists_dir,
                        NICKNAME="SomeBot", LIST_BASE_NAME="SomeBot")

        lists_path = os.path.join(self.tmp, "lists.json")
        with io.open(lists_path, "w", encoding="utf-8") as handle:
            json.dump([
                {"name": "Main", "primary": True, "channels": [], "folders": []},
                {"name": "movies-tv", "primary": False,
                 "channels": ["#somechannel"], "folders": []},
            ], handle)
        self.set_config(LISTS_FILE=lists_path)

        # The primary's list, with nothing in it but a header.
        self.write(os.path.join(self.lists_dir, "SomeBot-2026-09-07.txt"),
                   "List of 0 Files (0.00B)\n")
        # The second list's, holding the content.
        self.write(
            os.path.join(self.lists_dir, "movies-tv",
                         "SomeBot-VIDEO-2026-09-07.txt"),
            "List of 2 Films\n"
            + "=" * 30 + "\n"
            + "D:\\MEDIA\\Some Film (2020)\n"
            + "=" * 30 + "\n"
            + "!SomeBot Some.Film.2020.mkv  ::INFO:: 3000000000\n"
            + "!SomeBot Some.Other.2019.mkv  ::INFO:: 2000000000\n")

    def write(self, path, text):
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def sources(self):
        return [row["bot"] for row in webserver.build_own_list_summaries()]

    def rows_for(self, name):
        payload = webserver.build_filelists_payload(
            name=webserver.requested_own_list(name))
        return [row for group in (payload.get("folders") or [])
                for row in group.get("entries", [])]

    # ---------------------------------------------------------------- sidebar

    def test_both_lists_have_a_row(self):
        """The symptom, at the level it was reported."""
        self.assertEqual(self.sources(), ["__own__", "__own__:movies-tv"])

    def test_each_carries_its_own_name_once_there_is_more_than_one(self):
        labels = [row["label"] for row in webserver.build_own_list_summaries()]

        self.assertEqual(labels, ["Main", "movies-tv"])

    def test_they_are_marked_as_ours(self):
        """The page must not offer to re-fetch, download or pack a list we
        wrote ourselves."""
        for row in webserver.build_own_list_summaries():
            self.assertTrue(row["own"])
            self.assertEqual(row["freshness"], "own")
            self.assertTrue(row["held"])

    def test_the_operators_order_is_kept(self):
        """They arranged it; the page does not re-sort it alphabetically, and
        the primary stays first."""
        self.assertEqual(self.sources()[0], "__own__")

    # ---------------------------------------------------------------- content

    def test_the_second_lists_files_are_reachable(self):
        """The other half. A row to click is no use if it comes back empty."""
        rows = self.rows_for("movies-tv")

        self.assertEqual(len(rows), 2)
        self.assertIn("Some.Film.2020.mkv", [row["title"] for row in rows])

    def test_the_primary_is_still_the_primary(self):
        """The reporting operator's primary really was empty - that is not
        the bug, and the fix must not paper over it by merging the lists
        together."""
        self.assertEqual(self.rows_for(None), [])
        self.assertEqual(self.rows_for("Main"), [])

    def test_the_lists_do_not_bleed_into_each_other(self):
        self.assertNotEqual(self.rows_for("movies-tv"), self.rows_for("Main"))


class ARequestedListIsValidated(DCCoreTestCase):
    """?list= arrives from the wire and is joined into a directory path by
    list.find_latest_list()."""

    def test_a_name_we_do_not_serve_falls_back_to_the_primary(self):
        """Not an error: this route is polled continuously, and a list
        renamed or removed between one poll and the next would otherwise turn
        the table into an error message without anybody touching anything."""
        self.assertIsNone(webserver.requested_own_list("no-such-list"))

    def test_an_absent_parameter_means_the_primary(self):
        self.assertIsNone(webserver.requested_own_list(None))
        self.assertIsNone(webserver.requested_own_list(""))

    def test_a_traversal_attempt_does_not_reach_the_path_builder(self):
        self.assertIsNone(webserver.requested_own_list("../../etc"))
        self.assertIsNone(webserver.requested_own_list("..\\..\\windows"))


class TheSingleListInstallIsUnchanged(DCCoreTestCase):
    """Every install today serves exactly one list, called "Main" by a default
    the operator never chose. Showing them that name would be a change with no
    information in it."""

    def test_one_list_is_still_labelled_our_own_list(self):
        rows = webserver.build_own_list_summaries()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "Our own list")
        self.assertEqual(rows[0]["bot"], "__own__")

    def test_the_primary_needs_no_list_parameter(self):
        """GET /api/filelists with no ?list= meant the primary before lists
        had names, and still does."""
        self.assertIsNone(webserver.own_list_name("__own__"))

    def test_a_named_source_resolves_back(self):
        self.assertEqual(webserver.own_list_name("__own__:movies-tv"),
                         "movies-tv")

    def test_a_foreign_nick_is_not_ours(self):
        self.assertIsNone(webserver.own_list_name("someotherbot"))
        self.assertFalse(webserver.source_is_ours("someotherbot"))

    def test_both_forms_are_recognised_as_ours(self):
        self.assertTrue(webserver.source_is_ours("__own__"))
        self.assertTrue(webserver.source_is_ours("__own__:movies-tv"))


class ABrokenListsFileCostsTheExtraRowsOnly(DCCoreTestCase):
    """This route is polled every few seconds. A malformed lists.json must not
    take the whole sidebar down with it."""

    def test_it_still_answers_with_something_selectable(self):
        real = library.lists
        library.lists = lambda: (_ for _ in ()).throw(RuntimeError("bad json"))
        self.addCleanup(setattr, library, "lists", real)

        rows = webserver.build_own_list_summaries()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bot"], "__own__")

    def test_no_lists_at_all_still_offers_the_primary(self):
        real = library.lists
        library.lists = lambda: []
        self.addCleanup(setattr, library, "lists", real)

        self.assertEqual([row["bot"] for row in webserver.build_own_list_summaries()],
                         ["__own__"])


class ThePageAsksForTheRightOne(unittest.TestCase):

    def test_the_sidebar_no_longer_invents_our_row(self):
        """It cannot know how many lists there are or what they are called."""
        source = app_js()

        self.assertNotIn('botRow({ bot: "__own__", label: "Our own list"',
                         source)

    def test_a_named_list_is_asked_for_by_name(self):
        body = app_js().split("function loadFilelists()", 1)[1][:2000]

        self.assertIn('"?list=" + encodeURIComponent(listParam)', body)

    def test_every_one_of_ours_is_recognised_as_ours(self):
        """Missing one comparison would leave a second list looking like a
        foreign bot - fetchable, refetchable, and offered a Download button
        for a list we wrote ourselves."""
        source = app_js()
        # isOwnSource() is where the comparison is SUPPOSED to live, so its
        # own body is cut out before scanning - otherwise the guard fails on
        # the one implementation it exists to require.
        head, rest = source.split("function isOwnSource(", 1)
        outside = head + rest.split("\n  }", 1)[1]
        bare = [line.strip() for line in outside.splitlines()
                if '!== "__own__"' in line or '=== "__own__"' in line]

        self.assertEqual(bare, [],
                         "a bare __own__ comparison misses every list but the "
                         "primary - use isOwnSource()")

    def test_that_scan_can_still_see_the_rest_of_the_file(self):
        """Guard on the guard: cutting the helper out must not cut out
        everything after it, which would make the check above vacuous."""
        source = app_js()
        head, rest = source.split("function isOwnSource(", 1)
        outside = head + rest.split("\n  }", 1)[1]

        self.assertIn("function loadFilelists()", outside)
        self.assertIn("function folderHeadingHtml", outside)

    def test_the_helper_accepts_both_forms(self):
        body = app_js().split("function isOwnSource(", 1)[1].split("\n  }", 1)[0]

        self.assertIn('=== "__own__"', body)
        self.assertIn('indexOf("__own__:")', body)


if __name__ == "__main__":
    unittest.main()
