"""#399's follow-up: search the ONE list currently open in the List Browser.

Requested live, with a screenshot: a bot's archive can run to thousands of
rows (11,780 folders on the reporting install), and finding one file in it
meant paging through 200 rows at a time by hand. A search box under the
List Browser's tabs (#399) asks the same question `@find` and the Search tab
already answer, scoped to the single list open right now, rather than a
second search engine with its own idea of what "contains" means.

Deliberately NOT the sidebar's existing "Filter every list you hold" box
(#133): that one spans every list held at once and replaces the whole view
while a term is typed there. This one narrows the open list's own rows and
leaves the tabs, the pager and everything else untouched.
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
import list_fetch  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
        return handle.read()


def function_body(source, name):
    return source.split("function " + name + "(", 1)[1].split("\n  }", 1)[0]


def code_only(source):
    import re
    return re.sub(r"//[^\n]*", "", source)


class SplittingAQueryIntoSearchWords(unittest.TestCase):
    """The one place a raw "?q=" string becomes the word list
    find_matching_entries() takes - shared so a phrase means the same thing
    on the Search tab and in the List Browser's own per-list search."""

    def test_it_lowercases_and_splits_on_whitespace(self):
        self.assertEqual(webserver.split_list_search_words("Some Song"),
                         ["some", "song"])

    def test_dashes_and_underscores_count_as_spaces(self):
        """The same punctuation-as-space rule build_search_payload() already
        applied, now shared rather than duplicated."""
        self.assertEqual(webserver.split_list_search_words("some-song_name"),
                         ["some", "song", "name"])

    def test_a_blank_query_is_an_empty_list(self):
        self.assertEqual(webserver.split_list_search_words(""), [])
        self.assertEqual(webserver.split_list_search_words("   "), [])

    def test_none_is_treated_as_blank(self):
        self.assertEqual(webserver.split_list_search_words(None), [])

    def test_build_search_payload_still_uses_it(self):
        """The Search tab's own function must not have quietly kept a second,
        divergent copy of the splitting rule after the extraction."""
        import inspect
        source = inspect.getsource(webserver.build_search_payload)
        self.assertIn("split_list_search_words(query)", source)


class SearchingOurOwnOpenList(DCCoreTestCase):
    """build_filelists_payload()'s new `q` parameter, against a real list
    file on disk - not a mock of find_matching_entries()."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-listsearch-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.set_config(LOCAL_LIST_DIR=self.tmp, LIST_BASE_NAME="SomeBot")
        self.write(
            os.path.join(self.tmp, "SomeBot-2026-09-07.txt"),
            "List of 2 Files (11.00MB)\n\n"
            + "=" * 20 + "\nD:\\MUSIC\\Some Album\\\n" + "=" * 20 + "\n"
            + "!SomeBot Alpha Song.flac  ::INFO:: 5.00MB\n"
            + "!SomeBot Beta Track.flac  ::INFO:: 6.00MB\n")

    def write(self, path, text):
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def titles(self, payload):
        return [row["title"] for group in (payload.get("folders") or [])
                for row in group.get("entries", [])]

    def test_a_matching_word_narrows_the_list(self):
        payload = webserver.build_filelists_payload(q="alpha")

        self.assertEqual(payload["total_files"], 1)
        self.assertEqual(self.titles(payload), ["Alpha Song.flac"])

    def test_a_non_matching_word_returns_nothing(self):
        payload = webserver.build_filelists_payload(q="nonexistent")

        self.assertEqual(payload["total_files"], 0)
        self.assertEqual(payload["folders"], [])

    def test_a_blank_query_behaves_exactly_like_no_query_at_all(self):
        """find_matching_entries() already treats an empty word list as
        "match everything" - this must not grow a second meaning for
        "nothing typed"."""
        with_blank = webserver.build_filelists_payload(q="")
        without = webserver.build_filelists_payload()

        self.assertEqual(with_blank, without)

    def test_it_composes_with_paging(self):
        """The total the pager reads must be the FILTERED total, not the
        whole list's - otherwise "Folders 1-1 of 1" would be true of the
        search and false of what is actually on screen."""
        payload = webserver.build_filelists_payload(q="song", limit=1)

        self.assertEqual(payload["total_files"], 1)
        self.assertEqual(payload["total"], 1)


class SearchingAFetchedBotsOpenList(DCCoreTestCase):
    """build_fetched_bot_list_payload()'s new `q`, through
    list_fetch.get_fetched_bot_page() - the path a bot with tens of thousands
    of rows actually takes, since its rows are re-parsed from disk on every
    call rather than kept in memory (issue #76)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-listsearch-fetched-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        list_path = os.path.join(self.tmp, "OtherBot-2026-09-07.txt")
        with io.open(list_path, "w", encoding="utf-8") as handle:
            handle.write(
                "List of 2 Files (11.00MB)\n\n"
                + "=" * 20 + "\nD:\\MUSIC\\Some Album\\\n" + "=" * 20 + "\n"
                + "!OtherBot Gamma Song.flac  ::INFO:: 5.00MB\n"
                + "!OtherBot Delta Track.flac  ::INFO:: 6.00MB\n")
        config.fetched_bot_lists["otherbot"] = {
            "bot": "OtherBot", "fetched_at": 1, "list_path": list_path,
            "entry_count": 2, "advert_when_fetched": {},
        }

    def titles(self, payload):
        return [row["title"] for group in (payload.get("folders") or [])
                for row in group.get("entries", [])]

    def test_a_matching_word_narrows_the_list(self):
        status, payload = webserver.build_fetched_bot_list_payload(
            "otherbot", q="gamma")

        self.assertEqual(status, 200)
        self.assertEqual(payload["total_files"], 1)
        self.assertEqual(self.titles(payload), ["Gamma Song.flac"])

    def test_a_non_matching_word_returns_nothing_not_an_error(self):
        status, payload = webserver.build_fetched_bot_list_payload(
            "otherbot", q="nonexistent")

        self.assertEqual(status, 200)
        self.assertEqual(payload["total_files"], 0)

    def test_get_fetched_bot_page_takes_pre_split_words_directly(self):
        """The lower layer must not do its own splitting - webserver.py
        already did that once, from the raw query string."""
        entry = config.fetched_bot_lists["otherbot"]
        page, total_folders, total_rows, _row_capped, error = list_fetch.get_fetched_bot_page(
            entry, 0, 50, search_words=["gamma"])

        self.assertIsNone(error)
        self.assertEqual(total_rows, 1)

    def test_no_search_words_at_all_is_the_same_as_before_this_feature(self):
        """search_words=None (every existing caller) must still mean
        "everything" - the default this function had before #399's
        follow-up."""
        entry = config.fetched_bot_lists["otherbot"]
        page, total_folders, total_rows, _row_capped, error = list_fetch.get_fetched_bot_page(
            entry, 0, 50)

        self.assertIsNone(error)
        self.assertEqual(total_rows, 2)


class TheRoutesForwardQ(unittest.TestCase):
    """Both filelists routes read "?q=" and pass it through - a source-text
    check for the Flask wiring, since the payload-building functions above
    are already covered by real execution."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as h:
            return h.read()

    def test_the_own_list_route_forwards_q(self):
        body = self.source().split('@app.route("/api/filelists")', 1)[1][:600]
        self.assertIn('q=request.args.get("q", "")', body)

    def test_the_fetched_bot_route_forwards_q(self):
        body = self.source().split('@app.route("/api/filelists/bot/<nick>")', 1)[1][:400]
        self.assertIn('q=request.args.get("q", "")', body)


class TheFrontendSendsAndResetsTheQuery(unittest.TestCase):
    """Structural, like every other JS check in this suite. Covers the two
    ways this could quietly stop working: the query never reaching the
    request, or an old bot's term silently narrowing the next one."""

    def test_the_search_box_exists(self):
        with io.open(os.path.join(REPO_ROOT, "web", "index.html"), encoding="utf-8") as h:
            html = h.read()
        self.assertIn('id="filelists-list-search-input"', html)

    def test_load_filelists_appends_q_only_for_the_open_list_not_the_sidebar_filter(self):
        body = function_body(app_js(), "loadFilelists")
        # Present once, inside the browsing (non-sidebar-filter) branch.
        self.assertIn('url += "&q=" + encodeURIComponent(listQuery);', body)
        # And NOT folded into the sidebar-wide search URL, which answers a
        # different question across every list held.
        search_branch = body.split('url = "/api/filelists/search?q="', 1)[1][:200]
        self.assertNotIn("filelistsListQuery", search_branch)

    def test_switching_bots_resets_the_open_list_search(self):
        # Sliced to THIS ONE listener's own body - up to its closing "  });"
        # at the delegated-handler's own indent - not a fixed character
        # count, which a neighbouring handler's identical reset call could
        # satisfy even with this one's call deleted.
        click_handler = code_only(app_js()).split(
            'el.filelistsBotList.addEventListener("click"', 1)[1].split(
            "\n  });", 1)[0]
        self.assertIn("resetFilelistsListQuery();", click_handler)

    def test_switching_tabs_resets_the_open_list_search_too(self):
        tab_handler = code_only(app_js()).split(
            'el.filelistsListTabs.addEventListener("click"', 1)[1].split(
            "\n  });", 1)[0]
        self.assertIn("resetFilelistsListQuery();", tab_handler)

    def test_reset_bumps_the_debounce_token_not_only_the_value(self):
        """Clearing filelistsListQuery alone leaves a PENDING debounced call
        from the old list free to fire after the switch and narrow the new
        one by a term nobody typed there."""
        body = function_body(app_js(), "resetFilelistsListQuery")
        self.assertIn("filelistsListQueryToken += 1", body)


if __name__ == "__main__":
    unittest.main()
