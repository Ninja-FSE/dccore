"""The List Browser puts the sources beside the files, not above them.

Asked for during the beta, with the two regions drawn on a screenshot: the
sources should be a narrow column on the left and the table should sit beside
it, starting at the top.

WHAT IT REPLACED. Everything was stacked full-width, so a column of nicks -
which needs perhaps 300px - took the whole width, and the file table began some
600px down the page and then scrolled inside whatever height was left. On the
screen it was reported from, six file rows were visible under a source list
showing five.

THE minmax(0, 1fr) IS LOAD-BEARING. A grid track sized `1fr` takes
`min-width: auto`, so a wide table inside it pushes the TRACK wider rather than
scrolling within it - and the whole page gains a horizontal scrollbar, which is
worse than what was there before. A floor of 0 lets the column be narrower than
its content, handing the overflow back to .table-wrap where it is already
handled.

Nothing here executes CSS or opens a browser. These read the two files and
assert the decisions that are easy to lose in a later edit - especially that
floor, which looks like a redundant zero and is not.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def markup():
    with io.open(os.path.join(REPO_ROOT, "web", "index.html"),
                 encoding="utf-8") as handle:
        return handle.read()


def css():
    with io.open(os.path.join(REPO_ROOT, "web", "style.css"),
                 encoding="utf-8") as handle:
        return handle.read()


def filelists_section():
    return markup().split('<section class="view" id="view-filelists">', 1)[1] \
                   .split("</section>", 1)[0]


class TheTwoColumnsExist(unittest.TestCase):

    def test_there_is_a_layout_wrapper(self):
        self.assertIn('class="filelists-layout"', filelists_section())

    def test_the_sources_and_the_files_are_separate_columns(self):
        section = filelists_section()

        self.assertIn('class="filelists-sources"', section)
        self.assertIn('class="filelists-files"', section)

    def test_the_sources_come_first(self):
        """Left column. The operator picks a list, then reads it."""
        section = filelists_section()

        self.assertLess(section.index('class="filelists-sources"'),
                        section.index('class="filelists-files"'))

    def test_choosing_a_list_is_in_the_left_column(self):
        section = filelists_section()
        left = section.split('class="filelists-sources"', 1)[1] \
                      .split('class="filelists-files"', 1)[0]

        for part in ("filelists-fetch-input", "filelists-filter-input",
                     "filelists-bot-list", "bot-legend"):
            self.assertIn(part, left, part)

    def test_reading_one_is_in_the_right_column(self):
        section = filelists_section()
        right = section.split('class="filelists-files"', 1)[1]

        for part in ("filelists-table", "filelists-body", "folder-controls",
                     "filelists-pager", "filelists-freshness"):
            self.assertIn(part, right, part)


class NothingWasLostInTheMove(unittest.TestCase):
    """Every control the page had, still on it and still uniquely addressable -
    app.js finds all of these by id."""

    IDS = ("filelists-fetch-form", "filelists-fetch-input",
           "filelists-fetch-status", "filelists-filter-input",
           "filelists-filter-clear", "filelists-filter-status",
           "filelists-filter-actions", "filelists-filter-all",
           "filelists-filter-none", "filelists-bot-list",
           "filelists-freshness", "filelists-expand-all",
           "filelists-collapse-all", "filelists-download-selected-btn",
           "filelists-table", "filelists-body", "filelists-prev-btn",
           "filelists-page-info", "filelists-next-btn")

    def test_every_control_is_still_there_exactly_once(self):
        section = filelists_section()

        for name in self.IDS:
            with self.subTest(control=name):
                self.assertEqual(section.count('id="%s"' % name), 1)

    def test_the_whole_document_has_no_duplicate_of_them_either(self):
        """A copy left behind elsewhere would be found by getElementById
        instead, and would look like the page ignoring every click."""
        whole = markup()

        for name in self.IDS:
            with self.subTest(control=name):
                self.assertEqual(whole.count('id="%s"' % name), 1)

    def test_the_divs_balance(self):
        section = filelists_section()

        self.assertEqual(section.count("<div"), section.count("</div>"))


class TheGridCannotPushThePageWider(unittest.TestCase):

    def rule(self):
        block = css().split(".filelists-layout {", 1)[1]
        return block.split("}", 1)[0]

    def test_it_is_a_two_column_grid(self):
        rule = self.rule()

        self.assertIn("display: grid", rule)
        self.assertIn("grid-template-columns", rule)

    def test_the_file_column_has_a_floor_of_zero(self):
        """A bare `1fr` takes min-width:auto, so the table pushes the TRACK
        wider instead of scrolling inside it, and the whole page gains a
        horizontal scrollbar. This zero looks redundant and is the opposite."""
        self.assertIn("minmax(0, 1fr)", self.rule())

    def test_the_columns_themselves_may_shrink_too(self):
        """The floor on the track is undone if the item in it will not
        shrink."""
        stylesheet = css()

        for selector in (".filelists-sources", ".filelists-files"):
            block = stylesheet.split(selector + " {", 1)[1].split("}", 1)[0]
            self.assertIn("min-width: 0", block, selector)

    def test_the_table_still_owns_its_own_overflow(self):
        """Where the width is handed back to. Without this the floor above
        would only move the problem."""
        block = css().split(".table-wrap {", 1)[1].split("}", 1)[0]

        self.assertIn("overflow-x: auto", block)


class ItStacksWhenThereIsNoRoom(unittest.TestCase):

    def narrow_screen_block(self):
        """(condition, body) for each @media that restacks the List Browser.

        Brace-MATCHED. The first version split on "@media" and took everything
        after the next "{", which is the rest of the file - so it found the
        rule inside the first media query in the stylesheet and reported on
        that. A block has to be read as a block.

        Found by what it contains rather than by matching indented text: a
        rule sitting at the top level with two spaces in front of it would
        look identical and would apply at every width.
        """
        stylesheet = css()
        blocks = []
        at = stylesheet.find("@media")
        while at != -1:
            opened = stylesheet.index("{", at)
            depth, i = 0, opened
            while i < len(stylesheet):
                if stylesheet[i] == "{":
                    depth += 1
                elif stylesheet[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            body = stylesheet[opened + 1:i]
            if ".filelists-layout" in body:
                blocks.append((stylesheet[at:opened].strip(), body))
            at = stylesheet.find("@media", i)
        return blocks

    def test_that_reader_finds_real_blocks(self):
        """Guard on the guard: a matcher that returned the whole file, or
        nothing, would make every assertion below meaningless."""
        stylesheet = css()
        every = []
        at = stylesheet.find("@media")
        while at != -1:
            opened = stylesheet.index("{", at)
            depth, i = 0, opened
            while i < len(stylesheet):
                if stylesheet[i] == "{":
                    depth += 1
                elif stylesheet[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            every.append(stylesheet[opened + 1:i])
            at = stylesheet.find("@media", i)

        self.assertGreater(len(every), 3, "the stylesheet has several")
        self.assertTrue(all(len(b) < len(stylesheet) * 0.5 for b in every),
                        "a block ran past its own closing brace")

    def test_there_is_a_narrow_screen_fallback(self):
        """Below some width the two columns are narrower than either wants.
        Stacked is what was there before, which is the right thing to fall
        back to rather than a squeeze."""
        blocks = self.narrow_screen_block()

        self.assertEqual(len(blocks), 1,
                         "expected exactly one media query to restack the "
                         "List Browser")

    def test_the_fallback_is_a_maximum_width(self):
        """A min-width query would stack the WIDE screens instead, which is
        the same rule doing the opposite thing."""
        condition, _body = self.narrow_screen_block()[0]

        self.assertIn("max-width:", condition)
        self.assertNotIn("min-width:", condition)

    def test_the_fallback_is_one_column(self):
        _condition, body = self.narrow_screen_block()[0]

        self.assertIn("grid-template-columns: minmax(0, 1fr)", body)

    def test_the_source_list_gets_its_height_back_when_stacked(self):
        """Its cap exists because it used to sit ABOVE the table and every
        pixel it took came off the files. Side by side it costs the table
        nothing; stacked, it costs again."""
        stylesheet = css()

        self.assertIn("max-height: 60vh", stylesheet)
        self.assertIn("max-height: 216px", stylesheet)


class TheStylesheetStaysThemeable(unittest.TestCase):
    """Every colour in this file comes from the palette - a literal is wrong
    in one theme or the other."""

    def test_the_new_rules_introduce_no_colour_of_their_own(self):
        stylesheet = css()
        block = stylesheet.split("THE LIST BROWSER IS TWO COLUMNS", 1)[1]
        block = block.split(".bot-list {", 1)[0]

        self.assertNotIn("#", block)
        self.assertNotIn("rgb(", block)


if __name__ == "__main__":
    unittest.main()
