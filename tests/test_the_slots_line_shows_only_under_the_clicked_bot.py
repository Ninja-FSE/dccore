"""The slots/queue/speed line is shown under the bot you clicked, and only there (#943).

#932 put each online bot's advertised free slots, queue and speed on its own
sidebar row. Beside the name, in the same flex row, it squeezed the nick out on
a narrow sidebar - rows with the line and no readable name - and on every row
at once it was a wall of small print.

Now the line is out of the layout until its bot is clicked, then sits on a line
of its own under the row. A click on another bot closes the first; a click on
the open one closes it. The state lives in `state.filelistsInfoNick` because
the rows are rebuilt on every poll.

Two halves. The source guards read app.js and the stylesheet, the way this
suite's other sidebar guards do. The behaviour test runs the real click
fragment and `markFilelistsInfoBot()` out of app.js under node against stub
rows - a guard that reads text cannot tell that the toggle actually toggles -
and is skipped where node is not installed, the way the dashboard tests are
where Flask is not.
"""

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def css_rule(css, selector):
    """The declaration block of the first rule whose selector is exactly
    `selector` (so ".bot-row" does not match ".bot-row-live")."""
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return match.group(1) if match else None


class TheLineIsOutOfTheLayoutUntilItsBotIsClicked(unittest.TestCase):

    def setUp(self):
        self.css = read("web", "style.css")

    def test_it_is_hidden_by_default(self):
        rule = css_rule(self.css, ".bot-row-live")
        self.assertIsNotNone(rule)
        self.assertRegex(rule, r"display:\s*none")

    def test_the_clicked_row_shows_it(self):
        self.assertRegex(
            self.css,
            r"\.bot-row\.is-info-open\s+\.bot-row-live\s*\{\s*display:\s*block;?\s*\}")

    def test_it_takes_a_line_of_its_own_instead_of_squeezing_the_name(self):
        line = css_rule(self.css, ".bot-row-live")
        self.assertRegex(line, r"flex:\s*0\s+0\s+100%")
        self.assertRegex(css_rule(self.css, ".bot-row"), r"flex-wrap:\s*wrap")

    def test_it_comes_last_so_the_count_stays_on_the_names_line(self):
        """botRow() appends the file count AFTER this line. A full-width item
        that wraps takes its own line in markup order, so without `order` the
        count went to a third line under it - reported from the operator's own
        dashboard on the first try - instead of staying on the name's line at
        the right."""
        self.assertRegex(css_rule(self.css, ".bot-row-live"), r"order:\s*1\b")

    def test_the_gap_between_the_two_lines_is_small(self):
        """`gap` is both directions in a wrapping flex row: a bare `gap: 10px`
        would leave ten pixels between the name and its own slots line."""
        self.assertRegex(css_rule(self.css, ".bot-row"), r"gap:\s*2px\s+10px")


class TheClickOpensExactlyOneLine(unittest.TestCase):

    def setUp(self):
        self.js = read("web", "app.js")

    def test_the_state_is_kept_where_the_poll_redraw_cannot_lose_it(self):
        self.assertRegex(self.js, r'filelistsInfoNick:\s*""')

    def test_the_click_marks_its_bot_before_any_early_return(self):
        """A bot we only saw advertising has no list to switch to, and the
        handler returns for it - but its line is what you want before you
        decide to fetch one, so the marking has to come first."""
        handler = self.js.index('el.filelistsBotList.addEventListener("click"')
        marks = self.js.index("state.filelistsInfoNick = state.filelistsInfoNick === infoNick", handler)
        early = self.js.index('row.dataset.held === "no"', handler)
        self.assertLess(marks, early)

    def test_a_rebuilt_row_reads_the_state_back(self):
        start = self.js.index("function botRow(group)")
        body = self.js[start:self.js.index("function isOwnSource", start)]
        self.assertIn("state.filelistsInfoNick", body)
        self.assertIn('"is-info-open"', body)

    def test_the_line_stays_written_as_text(self):
        """The line is built from another bot's advert. Moving it must not
        turn it into markup."""
        at = self.js.index('stats.className = "bot-row-live";')
        self.assertIn("stats.textContent = live;", self.js[at:at + 120])
        self.assertNotIn("stats.innerHTML", self.js)


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const cut = (a, b) => {
  const i = src.indexOf(a); const j = src.indexOf(b, i);
  if (i < 0 || j < 0) { throw new Error("marker missing: " + a); }
  return src.slice(i, j + b.length);
};
const clickFrag = cut("var infoNick = String(row.dataset.nick", "markFilelistsInfoBot();");
const markFn = cut("function markFilelistsInfoBot()", "\n  }\n");

function mkRow(nick, hasLive) {
  const cls = new Set(), attrs = {};
  return {
    dataset: { nick: nick },
    classList: { toggle: function (c, on) { if (on) { cls.add(c); } else { cls.delete(c); } } },
    querySelector: function (s) { return s === ".bot-row-live" && hasLive ? {} : null; },
    setAttribute: function (k, v) { attrs[k] = v; },
    attrs: attrs, cls: cls
  };
}
const rows = [mkRow("Alpha", true), mkRow("Beta", true), mkRow("Gamma", false)];
const state = { filelistsInfoNick: "" };
const el = { filelistsBotList: { querySelectorAll: function () { return rows; } } };
const click = new Function("state", "el", markFn + "\nreturn function (row) { " + clickFrag + " };")(state, el);
const open = function () {
  return rows.filter(function (r) { return r.cls.has("is-info-open"); })
    .map(function (r) { return r.dataset.nick; }).join(",");
};
const log = [];
log.push("start=" + open());
click(rows[0]); log.push("alpha=" + open());
click(rows[1]); log.push("beta=" + open());
click(rows[1]); log.push("betaAgain=" + open());
click(rows[2]); log.push("gamma=" + open());
click(rows[0]); log.push("alphaBack=" + open());
log.push("gammaAria=" + rows[2].attrs["aria-expanded"]);
log.push("betaAria=" + rows[1].attrs["aria-expanded"]);
log.push("alphaAria=" + rows[0].attrs["aria-expanded"]);
console.log(log.join("\n"));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class TheToggleActuallyToggles(unittest.TestCase):

    def run_harness(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(
                ["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        return dict(line.split("=", 1) for line in done.stdout.decode("utf-8").splitlines())

    def test_one_line_open_at_a_time_and_a_second_click_closes_it(self):
        seen = self.run_harness()

        self.assertEqual(seen["start"], "", "nothing is open until something is clicked")
        self.assertEqual(seen["alpha"], "Alpha")
        self.assertEqual(seen["beta"], "Beta", "clicking another bot closes the first one's line")
        self.assertEqual(seen["betaAgain"], "", "clicking the open bot closes it")
        self.assertEqual(seen["alphaBack"], "Alpha")

    def test_a_row_with_no_line_is_never_marked_expandable(self):
        seen = self.run_harness()

        self.assertEqual(seen["gammaAria"], "undefined")
        self.assertEqual(seen["alphaAria"], "true")
        self.assertEqual(seen["betaAria"], "false")


if __name__ == "__main__":
    unittest.main()
