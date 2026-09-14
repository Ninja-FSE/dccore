"""The Stats page's two "most downloaded" tables, side by side.

Reported live, with a screenshot: "Most downloaded - files" and "Most
downloaded - album folders" stacked full width, one under the other, pushing
whatever came after them a screen's height down for no reason either table
needed the whole row. A long filename would also force the table wider than
its column, so truncating the name is what actually lets two columns fit
side by side rather than one of them winning a fight over width.
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


def app_js():
    return read("app.js")


def function_body(source, name):
    return source.split("function " + name + "(", 1)[1].split("\n  }", 1)[0]


def code_only(source):
    """Comments stripped, so a check for a dangerous PATTERN cannot be
    satisfied by a comment merely discussing it - as the one right above the
    real fix in renderTopTable() does."""
    return re.sub(r"//[^\n]*", "", source)


class TheTwoTablesShareOneGridRow(unittest.TestCase):

    def test_both_tables_sit_inside_the_grid_container(self):
        html = read("index.html")
        wrap = html.split('class="stat-top-grid"', 1)[1].split(
            '<p class="stat-foot" id="st-foot">', 1)[0]

        self.assertIn('id="st-top-files"', wrap)
        self.assertIn('id="st-top-albums"', wrap)

    def test_the_grid_uses_shrinkable_tracks(self):
        """A bare 1fr track takes min-width:auto, so a long filename would
        grow the TRACK instead of truncating inside it - same reasoning
        .filelists-layout's own tracks already rely on."""
        css = read("style.css")
        block = css.split(".stat-top-grid {", 1)[1].split("}", 1)[0]

        self.assertIn("minmax(0, 1fr) minmax(0, 1fr)", block)

    def test_it_falls_back_to_stacked_on_a_narrow_screen(self):
        css = read("style.css")
        self.assertIn("@media (max-width: 700px)", css)
        narrow = css.split("@media (max-width: 700px)", 1)[1][:400]
        self.assertIn(".stat-top-grid", narrow)
        self.assertIn("minmax(0, 1fr);", narrow)


class LongNamesAreTruncatedNotLeftToWiden(unittest.TestCase):

    def test_the_table_is_fixed_layout(self):
        """Without table-layout: fixed, the column still grows to fit its
        longest row instead of respecting the width given to the count
        column - the one column meant to give way."""
        css = read("style.css")
        block = css.split(".stat-top-table {", 1)[1].split("}", 1)[0]

        self.assertIn("table-layout: fixed", block)

    def test_the_name_column_ellipsises_rather_than_wrapping_or_scrolling(self):
        css = read("style.css")
        block = css.split(".stat-top-table td:first-child {", 1)[1].split("}", 1)[0]

        self.assertIn("text-overflow: ellipsis", block)
        self.assertIn("white-space: nowrap", block)
        self.assertIn("overflow: hidden", block)

    def test_the_count_column_has_a_fixed_width(self):
        """Fixed layout divides space by the FIRST row's cell widths unless
        told otherwise - without an explicit width here the name column's
        long first entry could still claim most of it."""
        css = read("style.css")
        block = css.split(".stat-top-table .col-num {", 1)[1].split("}", 1)[0]

        self.assertRegex(block, r"width:\s*\d+px")


class TheFullNameSurvivesAsATooltip(unittest.TestCase):
    """A truncated name has to be discoverable somehow, and it must be set
    the way every other remote string in this file is."""

    def body(self):
        return function_body(app_js(), "renderTopTable")

    def test_the_full_name_is_offered_on_hover(self):
        self.assertIn(".title = rows[i].name", self.body())

    def test_it_is_set_as_a_property_not_concatenated_into_an_attribute(self):
        """escapeHtml() encodes & < > and leaves a double quote alone, so a
        filename containing one could break out of a title="..." attribute
        built by string concatenation - the same rule every nick and
        filename elsewhere in this file already follows. Comments stripped
        first: the fix's own comment explains this exact risk in words, and
        a bare substring check would flag that explanation as the bug it is
        warning against."""
        body = code_only(self.body())

        self.assertNotIn("title=\\\"", body)
        self.assertNotIn('title="', body)


if __name__ == "__main__":
    unittest.main()
