"""A folder heading can select every file under it.

Asked for during the beta, looking at a nine-track album: an album is the unit
people actually want, and ticking nine boxes one at a time to get one is the
kind of work a page should be doing for them.

WHERE IT SITS. In the same column as the file checkboxes it commands, so the
relationship is visible rather than something to work out - which is why the
heading's cell is split into a check column and a colspan of four, instead of
spanning all five as it did.

THREE STATES, NOT TWO. Checked, unchecked, and INDETERMINATE for
some-but-not-all. The third is the honest one: a box reading "unchecked" while
four of nine rows are selected describes a selection that is not the one in
force.

A COLLAPSED FOLDER SELECTS TOO. Its rows are in the document already -
collapsing hides them rather than removing them - so a folder can be selected
without being opened, which is most of the point when a list has hundreds.

WHAT IT DOES NOT CLAIM. It selects the rows that are RENDERED. A folder past
the page's row ceiling arrives cut short and says so in its own row, and a box
that silently claimed the rest would be claiming to have queued files nobody
has seen.

Nothing here executes JavaScript, so these read the source. Where that is a
weak way to ask a question, the assertion is on the STATEMENT that has to be
there rather than on a name that could be renamed around it.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                 encoding="utf-8") as handle:
        return handle.read()


def function_body(name, source=None):
    body = (source or app_js()).split("function " + name + "(", 1)[1]
    return body.split("\n    }", 1)[0]


class TheHeadingCarriesABox(unittest.TestCase):

    def heading(self):
        return function_body("folderHeadingHtml")

    def test_there_is_a_folder_checkbox(self):
        self.assertIn("filelists-folder-check", self.heading())

    def test_it_is_in_the_same_column_as_the_files(self):
        """Split from the colspan that used to swallow the check column, so
        the box lines up under the ones it commands."""
        heading = self.heading()

        self.assertIn('class=\\"col-check\\"', heading)
        self.assertIn('colspan=\\"4\\"', heading)
        self.assertNotIn('colspan=\\"5\\"', heading)

    def test_it_appears_only_where_the_file_boxes_do(self):
        """A "select everything here" box over rows with no checkboxes to
        select is worse than either alone."""
        self.assertIn("rowsAreFetchable()", self.heading())

    def test_it_says_what_it_does(self):
        """The column is 32px wide and the box has no visible label."""
        heading = self.heading()

        self.assertIn("aria-label=", heading)
        self.assertIn("title=", heading)

    def test_the_folder_still_expands(self):
        """The heading is a button as well as a checkbox now, and the two
        must not have eaten each other."""
        heading = self.heading()

        self.assertIn("folder-toggle", heading)
        self.assertIn("folder-name", heading)
        self.assertIn("folder-count", heading)


class TickingItTicksTheFiles(unittest.TestCase):

    def test_a_folder_box_sets_every_row_in_that_folder(self):
        body = function_body("setFolderChecked")

        self.assertIn("boxes[i].checked = checked", body)

    def test_the_rows_are_found_by_walking_not_by_a_built_selector(self):
        """The index is our own loop counter, but it still reaches a
        selector - so it is matched by walking, the same rule the bot rows
        follow."""
        body = function_body("folderBoxes")

        self.assertIn("querySelectorAll(\"tr.file-row\")", body)
        self.assertIn("dataset.folderIndex !== String(index)", body)

    def test_a_collapsed_folder_is_still_reachable(self):
        """Its rows carry is-hidden and remain in the document, so the walk
        above finds them without the folder being opened. Asserted on the
        renderer, since that is what decides they stay.

        `is-hidden` is CONDITIONAL now - a list with no folders in it draws no
        heading, and rows with nothing to hide them under must not start
        hidden. What matters here is unchanged: where there IS a heading, its
        rows are in the document and reachable by index."""
        rows = function_body("folderFilesHtml")

        self.assertIn('(flat ? "" : " is-hidden")', rows)
        self.assertIn('"<tr class=\\"file-row"', rows)
        self.assertIn("data-folder-index=", rows)

    def test_the_shift_anchor_is_dropped(self):
        """"The last box the operator actually touched" is what a shift-range
        extends from. A folder box is not one of those, and a stale anchor
        would extend the next range from a row nobody pointed at."""
        body = function_body("setFolderChecked")

        self.assertIn("state.filelistsLastChecked = null", body)


class TheBoxFollowsItsFiles(unittest.TestCase):

    def sync(self):
        return function_body("syncFolderCheck")

    def test_all_selected_shows_checked(self):
        self.assertIn("checked === boxes.length", self.sync())

    def test_some_selected_shows_indeterminate(self):
        """The third state. Without it the box reads "unchecked" while four of
        nine rows are selected, describing a selection that is not the one in
        force."""
        body = self.sync()

        self.assertIn("box.indeterminate =", body)
        self.assertIn("checked > 0 && checked < boxes.length", body)

    def test_an_empty_folder_is_not_reported_as_fully_selected(self):
        """`0 === 0` is true, so a folder with no rows would show as checked
        without this."""
        self.assertIn("boxes.length > 0 &&", self.sync())

    def test_a_file_box_updates_its_folder(self):
        """Anchored on the FILELISTS listener by name. There is an earlier
        `addEventListener("change"` in this file - the broadcast table's - and
        an anchor that matched it would be reading a handler with no folders
        in it at all."""
        source = app_js()
        handler = source.split(
            'el.filelistsBody.addEventListener("change"', 1)[1][:1600]

        self.assertIn("syncFolderCheck(", handler)
        self.assertIn("filelists-folder-check", handler)

    def test_a_shift_range_updates_every_folder_it_crossed(self):
        """A range can span several folders, so every heading is re-read
        rather than only the one clicked in."""
        source = app_js()
        handler = source.split("SHIFT EXTENDS A RANGE", 1)[1][:2500]

        self.assertIn("syncEveryFolderCheck()", handler)


class ItDoesNotDisturbWhatWasThere(unittest.TestCase):

    def test_the_download_button_still_counts_file_boxes_only(self):
        """A folder box is not a file. Counting it would report one more
        selection than exists and enable the button with nothing queued."""
        body = function_body("updateFilelistsDownloadSelectedState")

        self.assertIn('querySelectorAll(".filelists-check:checked")', body)
        self.assertNotIn("folder-check", body)

    def test_the_shift_range_walks_file_boxes_only(self):
        """The two classes differ precisely so a range cannot sweep a heading
        into itself."""
        source = app_js()
        handler = source.split("SHIFT EXTENDS A RANGE", 1)[1][:2500]

        self.assertIn('querySelectorAll(".filelists-check")', handler)
        self.assertNotIn('querySelectorAll(".filelists-folder-check")', handler)

    def test_the_two_classes_are_distinct(self):
        """"filelists-check" is a prefix of nothing here by accident: a
        classList check for one must never match the other."""
        self.assertNotEqual("filelists-check", "filelists-folder-check")
        source = app_js()

        self.assertIn('contains("filelists-folder-check")', source)
        self.assertIn('contains("filelists-check")', source)


class TheCellIsStyled(unittest.TestCase):

    def css(self):
        with io.open(os.path.join(REPO_ROOT, "web", "style.css"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_the_folder_check_cell_has_a_rule(self):
        """`.folder-row td` sets padding to 0 so the toggle button can fill
        the row - which squashes a checkbox put in it."""
        self.assertIn(".folder-row td.col-check", self.css())

    def test_it_uses_no_colour_of_its_own(self):
        """Every colour in this file comes from the palette; a literal here
        would be wrong in one theme or the other."""
        block = self.css().split(".folder-row td.col-check", 1)[1][:400]

        self.assertNotIn("#", block)
        self.assertNotIn("rgb(", block)


if __name__ == "__main__":
    unittest.main()
