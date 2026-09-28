"""Ticking "Online only" takes the offline bots off the sidebar (#948).

#931 added the box beside the List Browser's filter, but it only reached
/api/filelists/search?online=1 - and that request is made only while a search
term is typed. With the filter box empty the box changed state and nothing else
on screen, which is what the operator saw: ticked, and the offline (red) bots
still listed.

Now the sidebar itself leaves out a bot that is KNOWN to be away, and redraws
the moment the box changes. Left in: our own lists, the bot whose list is open
(the table would otherwise show a list the sidebar has no row for), and a bot
whose presence is unknown - `null` is "has not finished joining", where the
membership mirror is empty and every nick would read as gone.

The source guards read app.js, as this suite's other sidebar guards do. The
behaviour test runs the real hiddenByOnlineOnly() - with the real
primaryEntry(), isOwnSource() and nickOfSource() beside it - under node, and is
skipped where node is not installed.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
        return handle.read()


def body_of(code, signature):
    """From `signature` to the end of its braces, by counting them - enough for
    the small helpers read here, none of which has a brace in a string."""
    start = code.index(signature)
    open_at = code.index("{", start)
    depth = 0
    for at in range(open_at, len(code)):
        if code[at] == "{":
            depth += 1
        elif code[at] == "}":
            depth -= 1
            if depth == 0:
                return code[start:at + 1]
    raise AssertionError("unbalanced braces after " + signature)


class TheSidebarAnswersTheBox(unittest.TestCase):

    def setUp(self):
        self.js = app_js()

    def test_the_box_is_state_and_starts_from_what_is_on_screen(self):
        self.assertIn("filelistsOnlineOnly: false, filelistsBotRows: null", self.js)
        at = self.js.index("if (el.filelistsOnlineOnly) {")
        self.assertIn("state.filelistsOnlineOnly = el.filelistsOnlineOnly.checked;",
                      self.js[at:at + 300])

    def test_ticking_it_redraws_the_sidebar_before_the_search(self):
        handler = self.js.index('el.filelistsOnlineOnly.addEventListener("change"')
        end = self.js.index("});", handler)
        body = self.js[handler:end]
        self.assertIn("renderFilelistsSwitcher(state.filelistsBotRows)", body)
        self.assertLess(body.index("renderFilelistsSwitcher("), body.index("runFilelistsFilter()"))

    def test_the_sidebar_leaves_the_row_out_but_keeps_the_bot(self):
        """state.filelistsBots is filled for EVERY bot before any row is left
        out: the rest of the page - the fetch box, the tabs, entriesForNick() -
        looks bots up there, and a bot that is merely not shown is still a bot."""
        body = body_of(self.js, "function renderFilelistsSwitcher(rows)")
        self.assertLess(body.index("state.filelistsBots[row.bot] = row;"),
                        body.index("hiddenByOnlineOnly("))
        self.assertIn("state.filelistsBotRows = rows;", body)
        self.assertIn("if (hiddenByOnlineOnly(groupsByNick[nickKey])) { return; }", body)


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function fn(signature) {
  const start = src.indexOf(signature);
  if (start < 0) { throw new Error("missing: " + signature); }
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") { depth++; }
    else if (src[i] === "}") { depth--; if (depth === 0) { break; } }
  }
  return src.slice(start, i + 1);
}
const code = ["function splitFetchedSource(", "function nickOfSource(", "function isOwnSource(",
  "function primaryEntry(", "function groupOnline(", "function displayNickOfSource(",
  "function hiddenByOnlineOnly("].map(fn).join("\n");
const make = new Function("state", code + "\nreturn hiddenByOnlineOnly;");

function group(nick, online, list) {
  return { nick: nick, entries: [{ bot: list || nick, nick: nick, online: online }] };
}
const out = [];
function ask(label, state, g) { out.push(label + "=" + make(state)(g)); }

ask("off_offline", { filelistsOnlineOnly: false, filelistsSource: "__own__", filelistsBots: {} }, group("Gone", false));
const on = { filelistsOnlineOnly: true, filelistsSource: "__own__", filelistsBots: {} };
ask("on_here", on, group("Here", true));
ask("on_gone", on, group("Gone", false));
ask("on_unknown", on, group("Joining", null));
ask("on_own_list", on, group("__own__", false));
ask("on_own_second_list", on, group("__own__:video", false, "__own__:video"));
ask("on_open_bot_gone", { filelistsOnlineOnly: true, filelistsSource: "GoneBot/rar", filelistsBots: {} },
    group("GoneBot", false));
ask("on_other_bot_gone", { filelistsOnlineOnly: true, filelistsSource: "SomeoneElse", filelistsBots: {} },
    group("GoneBot", false));
// #975: a bot merged into its new nick (#376) - the list held under the old
// nick, offline, and the new nick's advert row, online.
function merged() {
  return { nick: "NewNick", entries: [
    { bot: "OldNick", nick: "NewNick", online: false, held: true },
    { bot: "NewNick", nick: "NewNick", online: true }] };
}
ask("on_merged_here", on, merged());
const mergedOpen = { filelistsOnlineOnly: true, filelistsSource: "OldNick",
                     filelistsBots: { OldNick: { bot: "OldNick", nick: "NewNick" } } };
const gone = merged();
gone.entries[1].online = false;
ask("on_merged_gone_but_open", mergedOpen, gone);
ask("on_merged_gone", on, gone);
console.log(out.join("\n"));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class TheRealHelperDecidesWhichRowsGo(unittest.TestCase):

    def seen(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        return dict(line.split("=", 1) for line in done.stdout.decode("utf-8").splitlines())

    def test_nothing_goes_while_the_box_is_off(self):
        self.assertEqual(self.seen()["off_offline"], "false")

    def test_a_bot_that_is_here_stays(self):
        self.assertEqual(self.seen()["on_here"], "false")

    def test_a_bot_known_to_be_away_goes(self):
        self.assertEqual(self.seen()["on_gone"], "true")

    def test_a_bot_that_has_not_finished_joining_stays(self):
        """`null` is not "offline": nothing is known yet."""
        self.assertEqual(self.seen()["on_unknown"], "false")

    def test_our_own_lists_always_stay(self):
        seen = self.seen()

        self.assertEqual(seen["on_own_list"], "false")
        self.assertEqual(seen["on_own_second_list"], "false")

    def test_the_open_bot_stays_even_when_it_is_away_but_another_one_does_not(self):
        seen = self.seen()

        self.assertEqual(seen["on_open_bot_gone"], "false")
        self.assertEqual(seen["on_other_bot_gone"], "true")

    def test_a_bot_here_under_a_new_nick_stays(self):
        """#975: its held list is under the old nick, which is offline."""
        self.assertEqual(self.seen()["on_merged_here"], "false")

    def test_its_open_list_stays_by_the_nick_the_row_shows(self):
        seen = self.seen()

        self.assertEqual(seen["on_merged_gone_but_open"], "false")
        self.assertEqual(seen["on_merged_gone"], "true", "and gone when it really is gone")


if __name__ == "__main__":
    unittest.main()
