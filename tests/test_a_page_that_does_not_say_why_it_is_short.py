"""#477 - a correct page that reads exactly like a broken pager.

Reported live, with screenshots: an operator paging through a bot's list saw
31 folders, then 1, then 148 - "Folders 32-32 of 3 605" - and reasonably read
it as the pager being broken.

It is not. `list.page_folder_groups()` slices a page by FOLDER, then stops it
early once the folders on it would carry more ROWS than the response ceiling
allows; and it always returns at least one folder even when that folder alone
is over the ceiling, "because returning nothing would leave the caller unable
to advance". A list with one folder of ~4,200 files gets that folder as an
entire page, sandwiched between ordinary ones.

The server has computed `row_capped` for exactly this all along, and
webserver.py puts it in both payloads. Nothing read it. So the one thing the
operator saw was a sentence that is true and says nothing about why.

The sorting half of that report is NOT a defect and is not addressed here:
the order shown for a fetched peer's list is that peer's own order, verbatim,
because re-sorting somebody else's published list would mean showing
something other than what they actually offer.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list as list_mod  # noqa: E402

APP_JS = os.path.join(REPO_ROOT, "web", "app.js")


def app_source():
    with io.open(APP_JS, encoding="utf-8") as handle:
        return handle.read()


def code_only(text):
    """`text` with // comments taken out.

    Every assertion below is about a NAME that the comment beside it also
    mentions - `row_capped` appears in the paragraph explaining it - so a
    search of the raw file would pass on the prose alone.
    """
    return re.sub(r"//[^" + chr(10) + "]*", "", text)


def groups(sizes):
    """Folder groups in page_folder_groups()'s input shape."""
    return [{"folder": "#%02d" % index, "count": count,
             "entries": [{"filename": "f%d" % n} for n in range(count)]}
            for index, count in enumerate(sizes)]


class TheServerSaysWhyThePageIsShort(unittest.TestCase):
    """The half that already worked, pinned so the UI half below has
    something real to display."""

    def test_a_page_cut_short_by_the_ceiling_says_so(self):
        page, _folders, _rows, row_capped = list_mod.page_folder_groups(
            groups([10, 10, 4000, 10]), offset=0, limit=50, max_rows=2500)

        self.assertTrue(row_capped)
        self.assertLess(len(page), 4, "the ceiling did not cut this page")

    def test_one_folder_bigger_than_the_whole_ceiling_is_a_page_of_its_own(self):
        """The reported "Folders 32-32 of 3 605". Returning nothing would
        leave the caller unable to advance past it."""
        page, _folders, _rows, row_capped = list_mod.page_folder_groups(
            groups([4000, 10]), offset=0, limit=50, max_rows=2500)

        self.assertEqual(len(page), 1)
        self.assertTrue(row_capped)
        self.assertTrue(page[0].get("truncated"))
        self.assertEqual(page[0]["count"], 4000,
                         "the true size is still reported, so the view can say "
                         "'showing 2500 of 4000' rather than implying that is "
                         "all there is")

    def test_a_short_LAST_page_is_not_capped(self):
        """The discrimination that matters. Running out of folders and being
        cut short by the ceiling both produce a page smaller than `limit`, and
        only one of them needs explaining."""
        page, _folders, _rows, row_capped = list_mod.page_folder_groups(
            groups([10, 10, 10]), offset=0, limit=50, max_rows=2500)

        self.assertEqual(len(page), 3)
        self.assertFalse(row_capped)

    def test_no_ceiling_at_all_is_never_capped(self):
        """`max_rows` falsy is the valve switched off, and it returns early
        before any of the counting below. Nothing exercised that path, so a
        mutant making it report every page as capped survived - which would
        put the explanation on every page of a list that has no large folder
        in it at all."""
        for ceiling in (None, 0):
            with self.subTest(max_rows=ceiling):
                page, _folders, _rows, row_capped = list_mod.page_folder_groups(
                    groups([4000, 4000]), offset=0, limit=50, max_rows=ceiling)

                self.assertEqual(len(page), 2)
                self.assertFalse(row_capped)

    def test_an_ordinary_full_page_is_not_capped(self):
        page, _folders, _rows, row_capped = list_mod.page_folder_groups(
            groups([10] * 100), offset=0, limit=50, max_rows=2500)

        self.assertEqual(len(page), 50)
        self.assertFalse(row_capped)


class TheDashboardPassesItOn(unittest.TestCase):
    """webserver.py's half, which also already worked."""

    def test_both_payloads_carry_it(self):
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as handle:
            code = re.sub(chr(35) + "[^" + chr(10) + "]*", "", handle.read())

        self.assertEqual(code.count('"row_capped": row_capped'), 2,
                         "this bot's own list and a fetched peer's list are "
                         "paged by the same function and must explain "
                         "themselves the same way")


class TheViewFinallyReadsIt(unittest.TestCase):
    """The half that was missing, and the whole of #477's remaining work."""

    def test_the_payload_field_reaches_the_view(self):
        code = code_only(app_source())

        self.assertIn("payload.row_capped", code,
                      "the dashboard still ignores what the server tells it")

    def test_the_caption_explains_a_short_page(self):
        code = code_only(app_source())

        self.assertIn("if (state.filelistsRowCapped) {", code,
                      "nothing in the pager caption depends on it, so the "
                      "operator still sees a short page with no reason given")

    def test_the_explanation_is_attached_to_the_caption_and_not_dropped(self):
        """A string built and never assigned would satisfy a check for the
        words alone. Asserted as a SEQUENCE: the branch appends to `caption`,
        and `caption` is what the element is set to, after it."""
        code = code_only(app_source())
        body = code.split("function renderFilelistsPager(", 1)

        self.assertEqual(len(body), 2, "the pager caption has moved")
        pager = body[1].split(chr(10) + "    }", 1)[0]

        self.assertIn("caption +=", pager)
        self.assertIn("el.filelistsPageInfo.textContent = caption;", pager)
        self.assertLess(pager.index("caption +="),
                        pager.index("el.filelistsPageInfo.textContent = caption;"),
                        "the caption is assigned before the explanation is "
                        "added to it, so the explanation never appears")

    # THE ARRAY-SHAPE BRANCH HAS NO TEST, deliberately.
    #
    # loadFilelists() writes `Array.isArray(payload) ? false :
    # !!payload.row_capped`, and the branch is worth having because it says
    # out loud that an unpaged payload exists. It is not a GUARD: reading
    # `.row_capped` off an array gives undefined, and `!!undefined` is false,
    # so removing the check changes nothing an operator could see.
    #
    # A mutation run proved that - dropping the branch survived a test written
    # to protect it. Rather than keep an assertion that cannot fail for a real
    # regression, it is recorded here. There is no JS runtime in this suite, so
    # every app.js check in this file is a source read; a source read of
    # something with no behaviour behind it is the weakest thing in the file.
