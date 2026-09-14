"""#133's first two slices: the nav, and selecting a run of files at once.

Nothing in this project executes JavaScript, so everything here reads the
source. That is weaker than a real test and is stated plainly rather than
dressed up - but the two failures these guard against are both ones a reader
cannot see and the suite could not previously catch.

THE NAV (#133)

Queue is folded into Stats - "everything the bot knows about itself, with the
queue table keeping its place in the lower half of the view" - and the order
is by how often a view is used rather than by how central its information
feels: Search, Downloads, List Browser, Tools, Stats, then the two admin
surfaces.

That change touched three lists at once (`views` in app.js, the nav buttons
and the view sections), and a half-applied rename would have been invisible
until somebody clicked. tests/test_web_assets.py now checks the three agree;
this file checks the rename itself landed.

SHIFT-CLICK (#133)

Ctrl needs no code: a checkbox already toggles one box on its own. Shift has
to be handled on CLICK rather than change, because the range is computed
against the state BEFORE the browser toggles the clicked box - and because
shift-clicking across rows also selects the text between them, which looks
broken.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

WEB = os.path.join(REPO_ROOT, "web")


def read(name):
    with io.open(os.path.join(WEB, name), encoding="utf-8") as handle:
        return handle.read()


def without_comments(js):
    """Comments out before scanning. This project's comments quote the very
    constructs they warn against, and four guards written during the audit
    matched prose instead of code before the habit stuck."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in js.split("\n"))


class QueueIsPartOfStats(unittest.TestCase):

    def test_there_is_no_queue_tab(self):
        html = read("index.html")

        self.assertNotIn('data-view="queue"', html)
        self.assertNotIn('id="view-queue"', html)

    def test_the_queue_table_moved_into_the_stats_view(self):
        """Moved, not deleted: renderQueueTable() still targets these ids, and
        the sidebar status card reads the same endpoint on every view."""
        html = read("index.html")
        stats = html.split('id="view-stats"', 1)[1].split("</section>", 1)[0]

        for element_id in ("queue-body", "queue-table", "stat-slots",
                           "stat-files", "stat-users"):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', stats)

    def test_the_queue_poll_refreshes_the_table_where_it_now_lives(self):
        """The poll refreshes the visible table only when it is the one
        showing, so a background poll never clobbers what is being read. That
        condition had to follow the table to Stats."""
        js = without_comments(read("app.js"))

        self.assertNotIn('state.active === "queue"', js)
        # From the POLL, not from loadQueue(): both call
        # renderSidebarStatus(), and the first occurrence is the one-shot load
        # at init, which has no active-view condition to find.
        block = js.split('setInterval(function () {', 1)[1][:600]
        self.assertIn('state.active === "stats"', block)

    def test_nothing_still_tries_to_load_a_queue_view(self):
        js = without_comments(read("app.js"))

        self.assertNotIn('name === "queue"', js)


class TheTabsAreNamedForWhatTheyDo(unittest.TestCase):

    def test_the_labels(self):
        html = read("index.html")

        self.assertIn("List Browser", html)
        self.assertIn("Downloads", html)

    def test_the_titles_match_the_labels(self):
        """The nav button and the page heading are written in two different
        files; a rename applied to one reads as a bug in the other."""
        js = read("app.js")
        block = js.split("var views = {", 1)[1].split("};", 1)[0]

        self.assertIn('title: "List Browser"', block)
        self.assertIn('title: "Downloads"', block)

    def test_the_order_is_by_how_often_a_view_is_used(self):
        html = read("index.html")
        nav = html.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
        order = re.findall(r'data-view="([a-z]+)"', nav)

        self.assertEqual(order[:5],
                         ["search", "download", "filelists", "tools", "stats"],
                         "the daily work belongs at the top and Stats is a "
                         "glance; Settings and Console are occasional")
        # Messages sits with the glances rather than with the daily work:
        # somebody messaging the bot is common enough to want a standing
        # place, and rare enough that putting it above the List Browser would
        # push the day's work down for something looked at once a day.
        self.assertEqual(order[5:], ["messages", "settings", "console"])


class ShiftClickExtendsTheSelection(unittest.TestCase):

    def handler(self):
        """The SHIFT listener specifically.

        el.filelistsBody has more than one click listener - the folder toggle
        was there first - so splitting on the first addEventListener("click")
        finds the wrong one. `var box = evt.target;` opens this handler and
        nothing else.
        """
        js = without_comments(read("app.js"))
        return js.split("var box = evt.target;", 1)[1].split("\n    });", 1)[0]

    def test_it_is_handled_on_click_not_change(self):
        """The range is computed against the state BEFORE the browser toggles
        the clicked box, so change() is too late."""
        self.assertIn("shiftKey", self.handler())

    def test_the_range_takes_the_anchor_s_state(self):
        """"Extend the selection to here", not "toggle each one" - which is
        what every file manager means by shift-click."""
        handler = self.handler()

        self.assertIn("var wanted = anchor.checked;", handler)

    def test_the_text_selection_shift_would_make_is_cleared(self):
        """Shift-clicking across rows also selects the text between them,
        which reads as the page having broken."""
        handler = self.handler()

        self.assertIn("removeAllRanges", handler)

    def test_the_anchor_is_dropped_when_the_table_is_rebuilt(self):
        """It is a live DOM node. A rebuilt table leaves it pointing at an
        element no longer in the document, and indexOf() would never find
        it - so the next shift-click would silently do nothing."""
        js = without_comments(read("app.js"))
        attach = js.split("function attachFilelistsCheckboxData", 1)[1][:600]

        self.assertIn("state.filelistsLastChecked = null;", attach)

    def test_the_anchor_is_initialised(self):
        js = without_comments(read("app.js"))
        block = js.split("var state = {", 1)[1].split("};", 1)[0]

        self.assertIn("filelistsLastChecked", block)

    def test_ctrl_needs_no_code(self):
        """Recorded so nobody adds a ctrlKey branch that fights the browser: a
        checkbox already toggles exactly one box on its own."""
        self.assertNotIn("ctrlKey", read("app.js"))

    def test_the_operator_is_told_shift_works(self):
        """An interaction nobody can see is one nobody uses."""
        html = read("index.html")
        button = html.split('id="filelists-download-selected-btn"', 1)[1][:400]

        self.assertIn("Shift-click", button)


if __name__ == "__main__":
    unittest.main()
