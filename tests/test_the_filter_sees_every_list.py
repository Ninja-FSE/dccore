"""The cross-list filter searches every list, and says so about every list.

FROM THE BETA, filtering with two lists held from one bot:

    "why [a bot] - rar shows like it has a result for [a term] but i dont
     see any"

Because it was never asked. A bot's archive can hold several lists - its loose
files and its packed albums, or music and film - and since they started being
kept, each is indexed under its own name: "<nick>" for the main one and
"<nick>/<marker>" for the rest. The filter built its list of sources from the
NICKS alone.

Two consequences, and the reported one is the second:

  * the other lists were never searched, so a match inside one could not be
    found by the filter at all; and
  * they came back in neither `matched` nor `empty` - and the sidebar only
    dims what it is TOLD is empty, so a list with no matches was left bright,
    reading as the one list that had them.

The keys now come from the same place the sidebar's rows do, which is what
makes the two line up. That is the whole fix: one enumeration, used by both.

AND A SECOND THING, from the same screenshot: "why on folders i see only track
01 and track 06". That one is the filter working. "amon a" means "contains
amon AND contains a" - build_match_query()'s own rule, with the prefix
wildcard reserved for terms of two characters or more - and every result had a
standalone "A" in its title. But the folder heading said "2 files", which
means the folder's SIZE when browsing and the number of MATCHES when
filtering, in identical words. So it says "matches" when that is what it is.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "someotherbot"


def list_text(base, names, folder):
    out = ["Header\n", "=" * 30 + "\n", folder + "\n", "=" * 30 + "\n"]
    for name in names:
        out.append(f"!{base} {name}  ::INFO:: 5000000\n")
    return "".join(out)


class ABotWithTwoLists(DCCoreTestCase):
    """The main list holds a match; the second list holds none. That is the
    combination the report was made from, and the one an "is it empty?"
    answer has to get right."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-filter-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        config.FETCHED_FILES_DIR = self.tmp

        path = os.path.join(self.tmp, "incoming.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "SomeBot-Default(2026-01-02)-OS.txt",
                list_text("SomeBot", ["Bluebird Song.flac", "Other Thing.flac"],
                          "D:\\Music\\Album A"))
            archive.writestr(
                "SomeBot-rar(2026-01-02)-OS.txt",
                list_text("SomeBot", ["Nothing Relevant.flac"],
                          "D:\\Music\\Album B"))
        ok, reason = list_fetch.process_fetched_list_zip(BOT, path)
        self.assertTrue(ok, reason)

    def payload(self, term):
        return webserver.build_crosslist_search_payload(term)

    def sidebar_keys(self):
        return [row["bot"] for row in webserver.build_fetched_bot_list_summaries()
                if row.get("held")]

    def test_both_lists_are_asked_about(self):
        """Neither is left unmentioned - that is what left one bright."""
        payload = self.payload("bluebird")
        answered = {name.lower() for name in payload["matched"]} | {
            name.lower() for name in payload["empty"]}

        self.assertEqual(answered,
                         {key.lower() for key in self.sidebar_keys()})

    def test_the_list_with_no_match_is_reported_empty(self):
        """The reported symptom, at the level it was seen: this is what the
        sidebar dims on, and it was absent."""
        payload = self.payload("bluebird")

        self.assertIn(f"{BOT}/rar",
                      [name for name in payload["empty"]])

    def test_the_list_with_the_match_is_reported_matched(self):
        payload = self.payload("bluebird")

        self.assertIn(BOT, payload["matched"])
        self.assertNotIn(f"{BOT}/rar", payload["matched"])

    def test_a_match_inside_the_second_list_is_findable(self):
        """The other half, and the worse one: its contents were never
        searched, so nothing in it could be found at all."""
        payload = self.payload("relevant")

        self.assertIn(f"{BOT}/rar", payload["matched"])
        self.assertGreater(payload["total_files"], 0)

    def test_the_rows_come_back_from_it(self):
        payload = self.payload("relevant")
        titles = [row["title"] for group in payload["folders"]
                  for row in group["entries"]]

        self.assertIn("Nothing Relevant.flac", titles)

    def test_the_keys_are_the_sidebars_own(self):
        """What makes the dimming line up at all. Two enumerations of the
        same thing would drift, and drifting is what produced the report."""
        payload = self.payload("bluebird")
        answered = sorted(name.lower() for name in
                          list(payload["matched"]) + list(payload["empty"]))

        self.assertEqual(answered,
                         sorted(key.lower() for key in self.sidebar_keys()))


class ABotWithOneListIsUnchanged(DCCoreTestCase):
    """Every install before the archives started being kept whole, and every
    bot that publishes a single list."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-filter-one-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        config.FETCHED_FILES_DIR = self.tmp
        path = os.path.join(self.tmp, "incoming.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("SomeBot-2026-09-07.txt",
                             list_text("SomeBot", ["Bluebird Song.flac"],
                                       "D:\\Music\\Album A"))
        ok, reason = list_fetch.process_fetched_list_zip(BOT, path)
        self.assertTrue(ok, reason)

    def test_it_is_named_by_its_bare_nick(self):
        payload = webserver.build_crosslist_search_payload("bluebird")

        self.assertEqual(payload["matched"], [BOT])

    def test_an_entry_stored_before_lists_existed_still_works(self):
        """No "lists" key at all - written by an older build. It must still be
        asked about rather than dropped from the enumeration."""
        config.fetched_bot_lists["oldbot"] = {
            "bot": "oldbot", "fetched_at": 1, "list_path": "nowhere.txt",
            "entry_count": 3, "advert_when_fetched": {},
        }

        payload = webserver.build_crosslist_search_payload("bluebird")

        self.assertIn("oldbot", payload["empty"])


class TheFolderHeadingSaysWhichNumberItIs(unittest.TestCase):
    """"2 files" means the folder's SIZE when browsing and the number of
    MATCHES when filtering, in identical words - so a nine-track album whose
    title matched twice read as an album with two tracks in it."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def noun(self):
        body = self.source().split("function folderCountNoun(", 1)[1]
        return body.split("\n    }", 1)[0]

    def test_filtering_counts_matches(self):
        body = self.noun()

        self.assertIn('" match"', body)
        self.assertIn('" matches"', body)

    def test_browsing_still_counts_files(self):
        body = self.noun()

        self.assertIn('" file"', body)
        self.assertIn('" files"', body)

    def test_it_is_the_filter_that_decides(self):
        self.assertIn("state.filelistsFilter", self.noun())

    def test_the_heading_asks_it(self):
        heading = self.source().split("function folderHeadingHtml(", 1)[1]
        heading = heading.split("\n    }", 1)[0]

        self.assertIn("folderCountNoun(count)", heading)


if __name__ == "__main__":
    unittest.main()
