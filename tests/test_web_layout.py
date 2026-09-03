"""The chrome that must not scroll away.

Scroll into a folder with three hundred files and you used to lose the nav,
the page title and the column headings all at once - the shell was a plain
flex row with min-height:100vh, so the whole document scrolled as one and
nothing was pinned to anything.

The part worth writing down is why the obvious fix does nothing. .table-wrap
already carried overflow-x:auto, and CSS computes an "auto" on one axis into
an "auto" on the other, so the wrapper was ALREADY a scroll container in both
directions - with a height that grew to fit its content and therefore never
scrolled vertically. A sticky thead inside it sticks to that box, which moves
with the page, so it goes off the top of the screen exactly as before. The
wrapper has to gain a bounded height before a sticky heading inside it means
anything.
"""

import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO_ROOT, "web")


def css():
    with io.open(os.path.join(WEB, "style.css"), encoding="utf-8") as handle:
        return re.sub(r"/\*.*?\*/", "", handle.read(), flags=re.S)


def rule(selector):
    """The declarations of the first rule with this exact selector."""
    body = css()
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
        if match.group(1).strip() == selector:
            out = {}
            for decl in match.group(2).split(";"):
                if ":" in decl:
                    name, _, value = decl.partition(":")
                    out[name.strip()] = value.strip()
            return out
    return {}


class ThingsThatMustStayPut(unittest.TestCase):

    def test_the_nav_does_not_scroll_with_the_page(self):
        sidebar = rule(".sidebar")

        self.assertEqual(sidebar.get("position"), "sticky")
        self.assertEqual(sidebar.get("top"), "0")

    def test_the_nav_can_scroll_itself_on_a_short_viewport(self):
        """Seven items plus the footer on a laptop in a video call: pinned
        without its own overflow means the bottom entries are unreachable."""
        sidebar = rule(".sidebar")

        self.assertEqual(sidebar.get("height"), "100vh")
        self.assertEqual(sidebar.get("overflow-y"), "auto")
        self.assertEqual(sidebar.get("box-sizing"), "border-box",
                         "100vh plus padding overflows without this")

    def test_the_page_title_stays_on_every_view(self):
        """One header renders the title for all seven views, so this is one
        rule rather than seven."""
        topbar = rule(".topbar")

        self.assertEqual(topbar.get("position"), "sticky")
        self.assertEqual(topbar.get("top"), "0")

    def test_the_pinned_bars_are_opaque(self):
        """A transparent sticky bar is worse than none: rows scroll visibly
        through the text instead of behind an edge."""
        for selector in (".topbar", ".panel-bar", ".data-table thead th"):
            with self.subTest(selector=selector):
                self.assertIn("background", rule(selector), selector)


class TheColumnHeadingsStayWithTheRows(unittest.TestCase):

    def test_the_table_wrapper_is_a_bounded_scroll_region(self):
        """The whole reason a sticky heading works at all. Without a height
        limit the wrapper grows to fit, never scrolls, and the heading sticks
        to a box that is itself moving off the screen."""
        wrap = rule(".table-wrap")

        self.assertIn("max-height", wrap)
        self.assertEqual(wrap.get("overflow-y"), "auto")

    def test_the_headings_are_pinned(self):
        head = rule(".data-table thead th")

        self.assertEqual(head.get("position"), "sticky")
        self.assertEqual(head.get("top"), "0")

    def test_a_table_under_a_panel_bar_is_offset_below_it(self):
        """.panel-bar is INSIDE the wrapper and pinned too, so a heading at
        top:0 would sit underneath it rather than below it."""
        offset = rule(".panel-bar + .data-table thead th")

        self.assertEqual(offset.get("top"), "var(--panel-bar-h)")

    def test_the_panel_bar_is_pinned_because_it_carries_a_control(self):
        bar = rule(".panel-bar")

        self.assertEqual(bar.get("position"), "sticky")
        self.assertEqual(bar.get("top"), "0")

    def test_the_bars_stack_in_the_right_order(self):
        """Three pinned things can overlap. The heading must pass under the
        panel bar, and both under the topbar."""
        def z(selector):
            return int(rule(selector).get("z-index", "0"))

        self.assertGreater(z(".panel-bar"), z(".data-table thead th"))
        self.assertGreaterEqual(z(".topbar"), z(".panel-bar"))


class ItAppliesEverywhereItShould(unittest.TestCase):
    """Seven views, five of them with a table. A fix applied to the one view
    somebody happened to be looking at is half a fix."""

    def html(self):
        with io.open(os.path.join(WEB, "index.html"), encoding="utf-8") as handle:
            return handle.read()

    def test_every_data_table_sits_in_a_wrapper_that_scrolls(self):
        """The pinning is attached to .table-wrap and .data-table, so a table
        outside one gets neither."""
        html = self.html()
        wrapped = len(re.findall(r'class="[^"]*table-wrap', html))
        tables = len(re.findall(r'<table[^>]*class="[^"]*data-table', html))

        self.assertGreaterEqual(wrapped, tables,
                                "a data-table is not inside a .table-wrap, so its "
                                "headings cannot stick to anything")

    def test_the_views_that_have_tables_are_the_ones_expected(self):
        """Fixture invariant. If a view gains a table later this fails, which
        is the point - it is a prompt to check the new one scrolls too."""
        html = self.html()
        with_tables = sorted(
            m.group(1) for m in re.finditer(r'id="view-([a-z-]+)"', html)
            if "<table" in html[m.start():html.find("</section>", m.start())])

        self.assertEqual(with_tables,
                         ["download", "filelists", "queue", "search", "stats"])


if __name__ == "__main__":
    unittest.main()
