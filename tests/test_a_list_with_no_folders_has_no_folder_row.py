"""A list with nothing to group is not drawn as one group.

Reported from the beta: "If a file list is a rar file list there are no
folders. When I click on a rar file list I see 1 folder that I have to expand.
That's not needed when there are no folders."

Quite right. A bot that publishes its albums as packs has one row per pack and
no directory structure to speak of, so the whole list arrived as a single
unnamed group and was drawn as one collapsed row reading "(no folder)" with
11,232 files behind it - a click to get past a grouping that groups nothing.

THE TEST IS THE LIST'S SHAPE, NOT ANYTHING ABOUT .rar. One group, and that
group unnamed, which is exactly what folderGroupsFrom() produces for a flat
list of any origin. A list that genuinely has ONE folder is not flat: it has a
name worth showing, and its heading says which folder the rows belong to.

WHAT MUST NOT BE LOST WITH THE HEADING. It carried the select-every-file-here
box, in the same column as the boxes it commands. A flat list has no heading,
so that box moves to the table header - which is where a table-wide select
belongs anyway - keeping the same class and the same data-folder-index, so the
handler that already exists needs no changes: every row in a flat list is in
group 0.

And the two controls that only make sense with folders go away while there are
none. Expand all and Collapse all over a table with nothing to expand are
worse than absent: they claim a structure the table does not have.

THAT LAST CLAIM WAS WRONG. The class and index carry over, but the change
LISTENER that reacts to them did not: it was delegated on el.filelistsBody,
and the moved checkbox now lives in a <th>, a sibling of filelistsBody rather
than something inside it - so a tick there never reached the handler at all.
Reported live, on a flat RAR list: the box showed checked and nothing below
it was. Fixed by attaching the same handler to
el.filelistsHeadCheck directly, and by teaching folderCheckFor() to look there
too, so an individual row ticked by hand still syncs the header box back.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def read(name):
    with io.open(os.path.join(REPO_ROOT, "web", name), encoding="utf-8") as f:
        return f.read()


def code_only():
    return re.sub(r"//[^\n]*", "", read("app.js"))


def function(name, source=None):
    """One function's body.

    The closing brace is at the function's OWN indent, which is not the same
    for all of them - app.js has top-level functions at two spaces and nested
    ones at four. Slicing on a fixed terminator stopped at the first `}` of an
    early return instead, which is a body that passes nothing and fails
    everything.
    """
    text = source if source is not None else code_only()
    head = text.index("function %s(" % name)
    line_start = text.rfind("\n", 0, head) + 1
    indent = " " * (head - line_start)
    return text[head:].split("\n" + indent + "}", 1)[0]


class WhatCountsAsHavingNoFolders(unittest.TestCase):

    def rule(self):
        return function("listIsFlat")

    def test_one_group_and_that_group_unnamed(self):
        body = self.rule()

        self.assertIn("groups.length === 1", body)
        self.assertIn("!groups[0].folder", body)

    def test_one_named_folder_is_not_flat(self):
        """It has a name worth showing, and the heading says which folder the
        rows below belong to. Only the UNNAMED single group is the placeholder
        this removes."""
        body = self.rule()

        self.assertNotIn("groups.length <= 1", body)
        self.assertIn("&&", body)

    def test_the_placeholder_it_replaces_still_exists_for_the_other_case(self):
        """A file with no folder heading inside a list that HAS folders still
        has to be shown under something. The text is a translation key - see
        web/lang/en.json's filelists.noFolder."""
        self.assertIn('t("filelists.noFolder")', function("folderLabel"))


class TheTableWhenThereAreNoFolders(unittest.TestCase):

    def render(self):
        return function("renderFilelistGroups", code_only())

    def test_no_heading_row_is_drawn(self):
        self.assertIn('flat ? "" : folderHeadingHtml(group, index)', self.render())

    def test_the_rows_do_not_start_hidden(self):
        """There is no heading to hide them under - they ARE the table."""
        self.assertIn('(flat ? "" : " is-hidden")', function("folderFilesHtml"))

    def test_and_are_not_indented_under_nothing(self):
        self.assertIn('(flat ? "" : " col-indent")', function("folderFilesHtml"))

    def test_the_shape_is_decided_once_and_passed_down(self):
        """Rather than each renderer working it out again - two answers to
        "is this flat" is a heading with no rows, or rows with no heading."""
        render = self.render()

        self.assertIn("var flat = listIsFlat(groups);", render)
        self.assertIn("folderFilesHtml(group, index, flat)", render)

    def test_the_truncation_notice_says_list_rather_than_folder(self):
        """It is the only line left that would name a folder, and in a flat
        list there is not one to name. The wording is a translation key, not
        literal text - see web/lang/en.json's
        filelists.truncatedInList/truncatedInFolder."""
        body = function("folderFilesHtml")

        self.assertIn('flat ? "filelists.truncatedInList" : "filelists.truncatedInFolder"',
                      body)


class TheSelectAllMovesRatherThanDisappearing(unittest.TestCase):

    def controls(self):
        return function("renderFlatListControls", code_only())

    def test_it_goes_to_the_table_header(self):
        body = self.controls()

        self.assertIn("el.filelistsHeadCheck", body)
        self.assertIn("filelists-folder-check", body)

    def test_the_header_cell_exists_to_receive_it(self):
        self.assertIn('id="filelists-head-check"', read("index.html"))

    def test_it_keeps_the_class_and_index_the_handler_already_knows(self):
        """Every row in a flat list is in group 0, so the existing
        select-a-folder handler works unchanged."""
        body = self.controls()

        self.assertIn('data-folder-index="0"', body)

    def test_it_is_only_offered_where_the_rows_have_boxes(self):
        """A select-all over rows with no checkboxes is the same lie the
        folder heading is careful not to tell."""
        self.assertIn("flat && rowsAreFetchable()", self.controls())

    def test_and_is_cleared_when_the_list_has_folders_again(self):
        """The header cell is static markup: left filled, it would offer a
        select-all beside a table whose selects live on its headings."""
        body = self.controls()

        self.assertIn(': ""', body)

    def test_a_listener_is_actually_attached_to_the_header_box(self):
        """The defect: the class and the index moved, but the delegated
        listener that reacts to them was left behind on filelistsBody - a
        sibling of the <th> this checkbox now lives in, so a change event
        starting there never bubbled to it. Ticking the class and index
        alone proves nothing reads them."""
        self.assertIn("el.filelistsHeadCheck.addEventListener(\"change\"",
                      code_only())

    def test_the_header_listener_calls_the_same_select_all_helper(self):
        head = code_only().split(
            'el.filelistsHeadCheck.addEventListener("change"', 1)[1][:400]
        self.assertIn("setFolderChecked(", head)

    def test_syncing_a_ticked_row_back_can_find_the_header_box_too(self):
        """The other direction: ticking one row by hand has to be able to
        put the header box into its checked/indeterminate state, not only a
        folder heading inside filelistsBody."""
        body = function("folderCheckFor")
        self.assertIn("el.filelistsHeadCheck", body)


class TheControlsThatNeedFolders(unittest.TestCase):

    def test_expand_and_collapse_go_away(self):
        """Two buttons that do nothing claim the table has a structure it does
        not have."""
        body = function("renderFlatListControls", code_only())

        self.assertIn("el.filelistsExpandAll.hidden = flat;", body)
        self.assertIn("el.filelistsCollapseAll.hidden = flat;", body)

    def test_they_come_back(self):
        """Assigned from `flat` rather than set to true, so a list with
        folders after one without still has its buttons."""
        body = function("renderFlatListControls", code_only())

        self.assertNotIn("el.filelistsExpandAll.hidden = true;", body)


class ThePagerStopsCountingFolders(unittest.TestCase):

    def test_a_flat_list_is_described_in_files(self):
        """"Folders 1-1 of 1 (11,232 files)" is true and says nothing: the
        one group is everything, not one folder out of several.

        WINDOWED ON THE FUNCTION, not on the assignment. This used to slice
        600 characters forward from `el.filelistsPageInfo.textContent`, which
        worked only while that assignment was the first thing the caption did.
        #477 moved it to the END - the caption is built, then explained, then
        assigned - and the window opened on everything AFTER it instead."""
        body = function("renderFilelistsPager")

        self.assertIn("state.filelistsFlat", body)
        self.assertIn('"filelists.oneFile" : "filelists.manyFiles"', body)

    def test_the_shape_is_recorded_where_the_pager_can_see_it(self):
        """The pager runs from its own path, so it cannot call listIsFlat()
        on groups it does not hold."""
        self.assertIn("state.filelistsFlat = flat;",
                      function("renderFilelistGroups", code_only()))

    def test_it_starts_false(self):
        declared = read("app.js").split("filelistsFlat:", 1)[1].split(",", 1)[0]

        self.assertEqual(declared.strip(), "false")

    def test_a_list_with_folders_still_counts_them(self):
        """Windowed on the function - see the note above. The caption is a
        translation key - see web/lang/en.json's filelists.foldersRange."""
        body = function("renderFilelistsPager")

        self.assertIn('t("filelists.foldersRange")', body)


if __name__ == "__main__":
    unittest.main()
