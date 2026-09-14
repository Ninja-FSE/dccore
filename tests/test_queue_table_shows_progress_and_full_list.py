"""renderQueueTable() shows the whole queue and a progress bar, not just a
preview of the next file.

Nothing in this project executes JavaScript, so - the same as
test_dashboard_nav_and_multiselect.py - everything here reads the source.
That is weaker than a real test, stated plainly rather than dressed up: it
proves the wiring is present, not that a browser renders it correctly.

Requested live: "jag vill kunna se alla queue'ade filer, inte bara
'nästkommande' samt även en progressbar på dom aktuella sändningarna" - see
all queued files, not just the next one, and a progress bar on whatever is
actually sending. webserver.build_queue_payload() already carries "files"
(the full queue), "current_file", "bytes_sent" and "size" per row (see
tests/test_webserver.py's QueueRowsShowEveryFileAndSendingProgress) - this
file checks the dashboard actually uses them.
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
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in js.split("\n"))


class TheQueueTableReadsTheNewFields(unittest.TestCase):

    def setUp(self):
        self.js = without_comments(read("app.js"))

    def _render_queue_table_body(self):
        marker = "function renderQueueTable("
        self.assertIn(marker, self.js)
        start = self.js.index(marker)
        # The next top-level function definition ends this one, for a rough
        # but adequate slice - same technique test_announce_target_is_one_
        # channel.py uses for announce_worker().
        rest = self.js[start:]
        end = rest.index("\n  function ", 1)
        return rest[:end]

    def test_it_reads_the_full_file_list_not_just_preview_alone(self):
        body = self._render_queue_table_body()
        self.assertIn("row.files", body,
                     "the table only ever reads row.preview - the full "
                     "queue never reaches the page")

    def test_it_reads_the_currently_sending_file(self):
        body = self._render_queue_table_body()
        self.assertIn("row.current_file", body)

    def test_it_actually_calls_the_two_new_helpers(self):
        """Defining queueProgressBar()/queueFileList() proves nothing if
        renderQueueTable() never calls them."""
        body = self._render_queue_table_body()
        self.assertIn("queueProgressBar(row)", body)
        self.assertIn("queueFileList(row)", body)

    def test_it_reads_the_progress_fields(self):
        """Read inside queueProgressBar(row), which renderQueueTable() calls
        for a sending row - checked across the whole file rather than just
        renderQueueTable()'s own body, since that is where the reference
        actually lives."""
        self.assertIn("row.bytes_sent", self.js)
        self.assertIn("row.size", self.js)

    def test_a_single_queued_file_is_shown_plain_not_wrapped_in_details(self):
        """Only worth collapsing when there is more than one - a <details>
        around a single filename would just be a click for no reason."""
        self.assertIn("function queueFileList(", self.js)
        fn = self.js[self.js.index("function queueFileList("):]
        fn = fn[:fn.index("\n  function ", 1)]
        self.assertIn("files.length === 1", fn)

    def test_more_than_one_queued_file_can_all_be_opened(self):
        self.assertIn("function queueFileList(", self.js)
        fn = self.js[self.js.index("function queueFileList("):]
        fn = fn[:fn.index("\n  function ", 1)]
        self.assertIn("<details", fn)
        self.assertIn("<ul", fn)
        # Every entry, not a truncated head - map() over the whole array.
        self.assertIn("files.map(", fn)

    def test_the_progress_bar_reuses_the_existing_bar_markup(self):
        """The same .progress-bar/.progress-bar-fill pair the list-rebuild
        progress bar already uses (web/index.html's #update-list-bar), not a
        second, parallel implementation of the same idea."""
        self.assertIn("function queueProgressBar(", self.js)
        fn = self.js[self.js.index("function queueProgressBar("):]
        fn = fn[:fn.index("\n  function ", 1)]
        self.assertIn("progress-bar", fn)
        self.assertIn("progress-bar-fill", fn)

    def test_no_bar_is_drawn_when_the_size_is_not_known_yet(self):
        """None/undefined size (a brand-new dispatch, before dcc.py has read
        the file's real size off disk) must not render as a 0%-full bar,
        which would look identical to "just started" for a transfer whose
        progress genuinely cannot be known yet."""
        fn = self.js[self.js.index("function queueProgressBar("):]
        fn = fn[:fn.index("\n  function ", 1)]
        self.assertIn("if (!row.size)", fn)

    def test_the_percentage_is_clamped_to_100(self):
        """bytes_sent can briefly read past size on the very last chunk
        (the loop adds a chunk's length before checking completeness) - an
        unclamped bar must not visibly overshoot its own track."""
        fn = self.js[self.js.index("function queueProgressBar("):]
        fn = fn[:fn.index("\n  function ", 1)]
        self.assertIn("Math.min(100", fn)


class TheQueueStylesExist(unittest.TestCase):

    def setUp(self):
        self.css = read("style.css")

    def test_the_progress_bar_classes_are_defined(self):
        for selector in (".queue-current", ".queue-progress",
                         ".queue-files", ".queue-file-list"):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.css)


if __name__ == "__main__":
    unittest.main()
