"""The dashboard keys nothing a peer names into a plain object.

A nick is anybody's choice, and "constructor", "__proto__", "toString" and
"valueOf" are all legal ones. web/app.js grouped the List Browser's bots and
an @find broadcast's replies into plain {} objects keyed by nick, where those
names are already there - inherited from Object.prototype:

- a bot advertising as "constructor" or "__proto__" made the List Browser
  sidebar throw half-way through drawing it, so it came out empty (our own
  lists included), and every poll after that marked the dashboard as
  disconnected;
- with a filter active, such a bot's results were always hidden, and its
  toggle could not bring them back;
- a broadcast reply from "toString" emptied the results table and left its
  poll erroring every second; one from "__proto__" wrote Object.prototype
  itself, and every later broadcast in that tab showed 0 files per bot.

Every such map is now Object.create(null), and the broadcast poll stops
before it draws the replies, so one bad reply cannot keep it running.

The real functions run under node where it is installed (CI's runners have
it); the statements that carry the fix are also pinned by reading the source,
which runs everywhere.
"""

import io
import json
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

ODD = ["constructor", "__proto__", "toString", "valueOf", "hasOwnProperty"]


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
        return handle.read()


def function_body(source, name):
    return source.split("  function %s(" % name, 1)[1].split("\n  }\n", 1)[0]


def code_only(text):
    """`text` without its comment lines and /* */ blocks, so a comment that
    quotes the old line cannot satisfy or fail a check."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in text.split("\n")
                     if not line.lstrip().startswith("//"))


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const ODD = JSON.parse(process.argv[3]);
const NL = String.fromCharCode(10);
const fn = (name) => {
  const i = src.indexOf("  function " + name + "(");
  const j = src.indexOf(NL + "  }" + NL, i);
  if (i < 0 || j < 0) { throw new Error("missing " + name); }
  return src.slice(i, j + 4);
};
// The real initial value of state.filelistsExcluded, read off the state literal.
const excludedInit = /filelistsExcluded: ([^,]+),/.exec(src)[1];
const botsInit = /filelistsBots: ([^,]+),/.exec(src)[1];
const out = {};
const protoBefore = Object.getOwnPropertyNames(Object.prototype).sort().join(",");

// -- groupBroadcastResults: every sender is its own group, nothing inherited.
const group = new Function(fn("groupBroadcastResults") + NL + "return groupBroadcastResults;")();
function run(entries) {
  try { return { groups: group(entries).map((g) => [g.from, g.files.length, g.other.length, !!g.header]) }; }
  catch (e) { return { error: String(e) }; }
}
const replies = [{ from: "alfa", bot: "alfa", filename: "Song.flac" }];
ODD.forEach((nick) => {
  replies.push({ from: nick, header: { matches: 9 } });
  replies.push({ from: nick, bot: nick, filename: nick + ".flac" });
  replies.push({ from: nick, text: "hello" });
});
out.broadcast = run(replies);
// A later broadcast in the same tab, after one that named "__proto__".
out.broadcastAfter = run([{ from: "alfa", bot: "alfa", filename: "A.flac" },
                          { from: "bravo", bot: "bravo", filename: "B.flac" }]);
out.protoUntouched = Object.getOwnPropertyNames(Object.prototype).sort().join(",") === protoBefore
  && !("header" in {});

// -- the broadcast poll stops even when drawing the replies throws.
(async () => {
  const cleared = [];
  const broadcast = { pollTimer: 7, deadline: 0 };
  const poll = new Function("fetchJson", "markConnection", "renderBroadcastResults",
    "showBroadcastStatus", "broadcast", "clearInterval", "t",
    fn("pollBroadcastStatus") + NL + "return pollBroadcastStatus;")(
    () => Promise.resolve({ listening: false, results: [{ from: "toString" }] }),
    () => {}, () => { throw new TypeError("drawing failed"); },
    () => {}, broadcast, (id) => cleared.push(id), (k) => k);
  poll();
  await new Promise((resolve) => setTimeout(resolve, 50));
  out.pollStopped = { timer: broadcast.pollTimer, cleared: cleared };

  // -- the List Browser sidebar draws every bot, whatever it is called.
  const appended = [];
  const state = { filelistsSource: "__own__", filelistsBots: eval("(" + botsInit + ")"),
                  filelistsFilterPayload: null };
  const list = { scrollTop: 0, innerHTML: "x", appendChild: (n) => appended.push(n),
                 querySelectorAll: () => [] };
  const render = new Function("el", "state", "document", "hiddenByOnlineOnly", "botRow",
    "markFilelistsActiveBot", "applyFilterHighlight",
    fn("renderFilelistsSwitcher") + NL + "return renderFilelistsSwitcher;")(
    { filelistsBotList: list }, state, { activeElement: null }, () => false,
    (g) => ({ nick: g.nick, lists: g.entries.length }), () => {}, () => {});
  const rows = ["alfa"].concat(ODD, ["Constructor", "bravo"]).map((nick) =>
    ({ bot: nick, nick: nick, list: "", held: true, online: true }));
  rows.push({ bot: "constructor/rar", nick: "constructor", list: "rar", held: true, online: true });
  try { render(rows); out.sidebar = appended.map((r) => [r.nick, r.lists]); }
  catch (e) { out.sidebar = String(e); }
  out.sidebarBots = Object.keys(state.filelistsBots).sort();

  // -- a bot switched on or off by name, with the real initial value.
  const filter = new Function("state", "rerenderFromFilterPayload",
    [fn("splitFetchedSource"), fn("nickOfSource"), fn("visibleFilterGroups"),
     fn("setEveryListShown")].join(NL) +
    NL + "return { visible: visibleFilterGroups, every: setEveryListShown };");
  const filterState = { filelistsFilter: "song", filelistsExcluded: eval("(" + excludedInit + ")"),
                        filelistsFilterPayload: { matched: [] } };
  const api = filter(filterState, () => {});
  const groups = ODD.concat(["alfa"]).map((nick) => ({ bot: nick + "/rar" }));
  out.visibleAtStart = api.visible(groups).map((g) => g.bot);
  api.every(true);
  out.visibleAfterShowAll = api.visible(groups).map((g) => g.bot);
  filterState.filelistsFilterPayload = { matched: ODD.concat(["alfa"]) };
  api.every(false);
  out.visibleAfterShowNone = api.visible(groups).map((g) => g.bot);

  // -- the other nicks a merged row stands for.
  const others = new Function(fn("splitFetchedSource") + NL + fn("otherNicks") +
                              NL + "return otherNicks;")();
  out.otherNicks = others({ nick: "alfa", entries: ODD.map((nick) => ({ bot: nick })) });

  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class TheRealFunctionsUnderNode(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js"),
                                   json.dumps(ODD)], capture_output=True, timeout=120)
        finally:
            os.unlink(path)
        if done.returncode != 0:
            raise AssertionError(done.stderr.decode("utf-8", "replace"))
        cls.seen = json.loads(done.stdout.decode("utf-8"))

    def test_every_broadcast_sender_is_its_own_group(self):
        groups = self.seen["broadcast"].get("groups")
        self.assertIsNotNone(groups, self.seen["broadcast"])
        self.assertEqual(sorted(g[0] for g in groups), sorted(["alfa"] + ODD))
        for name, files, other, header in groups:
            if name != "alfa":
                self.assertEqual((files, other, header), (1, 1, True), name)

    def test_a_reply_cannot_change_what_every_object_has(self):
        self.assertTrue(self.seen["protoUntouched"])
        self.assertEqual(self.seen["broadcastAfter"],
                         {"groups": [["alfa", 1, 0, False], ["bravo", 1, 0, False]]})

    def test_the_poll_stops_even_when_drawing_the_replies_fails(self):
        self.assertEqual(self.seen["pollStopped"], {"timer": None, "cleared": [7]})

    def test_the_sidebar_draws_every_bot(self):
        sidebar = self.seen["sidebar"]
        self.assertIsInstance(sidebar, list, sidebar)
        # "Constructor" is "constructor" in another case: one bot, three lists.
        self.assertEqual(sidebar, [["alfa", 1], ["constructor", 3], ["__proto__", 1],
                                   ["toString", 1], ["valueOf", 1], ["hasOwnProperty", 1],
                                   ["bravo", 1]])
        self.assertIn("__proto__", self.seen["sidebarBots"])
        self.assertIn("constructor/rar", self.seen["sidebarBots"])

    def test_no_bot_starts_switched_off_and_each_can_be_switched(self):
        everyone = [nick + "/rar" for nick in ODD + ["alfa"]]
        self.assertEqual(self.seen["visibleAtStart"], everyone)
        self.assertEqual(self.seen["visibleAfterShowAll"], everyone)
        self.assertEqual(self.seen["visibleAfterShowNone"], [])

    def test_a_merged_row_names_every_other_nick(self):
        self.assertEqual(self.seen["otherNicks"], ODD)


class TheSourceSaysSo(unittest.TestCase):
    """The statements that carry the fix, for a runner without node."""

    MAPS = {
        "groupBroadcastResults": ["groups"],
        "renderFilelistsSwitcher": ["groupsByNick"],
        "otherNicks": ["seen"],
        "applyFilterHighlight": ["empty"],
        "renderQueueTable": ["opened"],
        "configuredChannels": ["seen"],
        "servedListChannelsHtml": ["boundKeys", "offeredKeys"],
        "channelNamesFor": ["offeredKeys"],
        "channelModes": ["out"],
    }

    def setUp(self):
        self.js = app_js()

    def test_each_name_keyed_map_has_no_prototype(self):
        for name, variables in self.MAPS.items():
            body = code_only(function_body(self.js, name))
            for variable in variables:
                with self.subTest(function=name, variable=variable):
                    self.assertIn("var %s = Object.create(null);" % variable, body)
                    self.assertNotRegex(body, r"\b%s\s*=\s*\{\s*\}" % re.escape(variable))

    def test_the_state_maps_have_no_prototype_anywhere_they_are_set(self):
        code = code_only(self.js)
        for field in ("filelistsBots", "filelistsExcluded"):
            with self.subTest(field=field):
                self.assertIn("    %s: Object.create(null)," % field, code)
                self.assertNotRegex(code, r"\b%s\s*[:=]\s*\{\s*\}" % field)
        self.assertEqual(code.count("state.filelistsExcluded = Object.create(null);"), 2)
        self.assertEqual(code.count("state.filelistsBots = Object.create(null);"), 1)

    def test_the_broadcast_poll_stops_before_it_draws(self):
        body = code_only(function_body(self.js, "pollBroadcastStatus"))
        stop = body.index("clearInterval(broadcast.pollTimer);")
        draw = body.index("renderBroadcastResults(payload.results);")
        self.assertLess(stop, draw)
        self.assertIn("if (!payload.listening && broadcast.pollTimer) {", body)


if __name__ == "__main__":
    unittest.main()
