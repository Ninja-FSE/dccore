"""The List Browser's filter highlight groups the held rows once per call.

#1141: while a filter was active, applyFilterHighlight() called
entriesForNick(nick) for every sidebar row, and entriesForNick() walked every
key of state.filelistsBots each time. That is O(rows x bots): about 18 ms on a
channel of 300 advertisers, over a second near the registry's 2,000-bot cap,
and it ran again on every 4-second sidebar poll and every filter answer, on
the browser's main thread.

The fix builds one nick -> rows map (entriesByNick()) at the top of each
applyFilterHighlight() call and hands it to entriesForNick(nick, byNick). The
map is a local of that call and is dropped with it: entriesForNick()'s own
comment explains why the rows are not mirrored in a second map kept in step
by hand, and a map rebuilt from state.filelistsBots on every call cannot
drift from it.

What is pinned here, run as the real functions out of app.js under node:
- the sidebar classes and the hidden count are the same as the old per-row
  scan gave, on varied seeded input (merged nicks, several lists per bot,
  mixed case, rows without a nick, nicks that are Object.prototype names);
- entriesForNick() with no map answers exactly what the old scan answered,
  in the same order, and hands back a fresh array each time;
- one applyFilterHighlight() call enumerates state.filelistsBots once, not
  once per row.
The node half skips where node is missing (CI's runners have it), so the
statements that carry the fix are also pinned by reading the source, which
runs everywhere.
"""

import io
import json
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


def function_body(source, name):
    return source.split("  function %s(" % name, 1)[1].split("\n  }\n", 1)[0]


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const NL = String.fromCharCode(10);
const fn = (name) => {
  const i = src.indexOf("  function " + name + "(");
  const j = src.indexOf(NL + "  }" + NL, i);
  if (i < 0 || j < 0) { throw new Error("missing " + name); }
  return src.slice(i, j + 4);
};
const shared = ["splitFetchedSource", "nickOfSource", "isOwnSource", "applyFilterHighlight"]
  .map(fn).join(NL);
const current = shared + NL + fn("entriesByNick") + NL + fn("entriesForNick");
// The scan #1141 replaced, verbatim: the reference every answer is held to.
const OLD = [
  "  function entriesForNick(nick) {",
  "    var nickLower = String(nick || '').toLowerCase();",
  "    var out = [];",
  "    Object.keys(state.filelistsBots).forEach(function (key) {",
  "      var row = state.filelistsBots[key];",
  "      if (String(row.nick || row.bot || '').toLowerCase() === nickLower) {",
  "        out.push(row);",
  "      }",
  "    });",
  "    return out;",
  "  }"].join(NL);
// And the highlight as it was before #1141: no map, one scan per row.
const oldHighlight = fn("applyFilterHighlight")
  .replace("var byNick = entriesByNick();", "")
  .replace("entriesForNick(nick, byNick)", "entriesForNick(nick)");
if (oldHighlight.indexOf("byNick") >= 0) { throw new Error("could not rebuild the old highlight"); }
const old = shared.replace(fn("applyFilterHighlight"), oldHighlight) + NL + OLD;

function rowEl(bot, nick) {
  const cls = new Set();
  return { dataset: { bot: bot, nick: nick },
    classList: { toggle: (c, on) => on ? cls.add(c) : cls.delete(c), has: (c) => cls.has(c) },
    contains: () => false, cls: cls };
}

function build(code, state, rows) {
  const el = { filelistsBotList: { querySelectorAll: () => rows },
               filelistsFilterStatus: { hidden: true, textContent: "" } };
  const reveal = {};
  const document = { activeElement: null };
  const t = (k) => k;
  const renderRevealButton = (filtering, hidden) => { reveal.hidden = hidden; };
  const api = new Function("state", "el", "document", "t", "renderRevealButton",
    code + NL + "return { applyFilterHighlight: applyFilterHighlight, " +
    "entriesForNick: entriesForNick, " +
    "entriesByNick: typeof entriesByNick === 'function' ? entriesByNick : null };")
    (state, el, document, t, renderRevealButton);
  api.reveal = reveal;
  return api;
}

let seed = 1141;
function rand(n) {
  // mulberry32: small, seeded, and even in its low bits, so every case is
  // the same on every run and every branch below gets taken.
  seed = (seed + 0x6D2B79F5) | 0;
  let x = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
  return ((x ^ (x >>> 14)) >>> 0) % n;
}
const ODD = ["constructor", "__proto__", "toString", "hasOwnProperty", "valueOf"];

function makeCase() {
  const bots = {};
  const sidebar = [];
  const count = 1 + rand(40);
  for (let b = 0; b < count; b++) {
    const base = b < ODD.length && rand(2) ? ODD[b] : "Bot" + b;
    const shown = rand(5) === 0 ? "Merged" + rand(4) : base;
    const lists = 1 + rand(3);
    for (let l = 0; l < lists; l++) {
      const key = l === 0 ? base : base + "/list" + l;
      const row = { bot: key, list: l === 0 ? "" : "list" + l, held: rand(4) !== 0 };
      if (shown !== base || rand(3) === 0) { row.nick = rand(2) ? shown : shown.toUpperCase(); }
      if (rand(10) === 0) { delete row.nick; row.bot = key.toLowerCase(); }
      bots[key] = row;
    }
    sidebar.push([base, shown]);
  }
  if (rand(3) === 0) { bots["__own__"] = { bot: "__own__", nick: "__own__", held: true }; sidebar.push(["__own__", "__own__"]); }
  const empty = Object.keys(bots).filter(() => rand(3) !== 0);
  const excluded = {};
  sidebar.forEach((s) => { if (rand(6) === 0) { excluded[s[1].toLowerCase()] = true; } });
  const open = sidebar.length && rand(2) ? sidebar[rand(sidebar.length)][0] : "";
  return { bots: bots, sidebar: sidebar, payload: { empty: empty },
    filter: rand(5) === 0 ? "" : "x", open: open, reveal: rand(4) === 0, excluded: excluded };
}

function runHighlight(code, c, countKeys) {
  let enumerations = 0;
  const bots = countKeys
    ? new Proxy(c.bots, { ownKeys(target) { enumerations++; return Reflect.ownKeys(target); } })
    : c.bots;
  const state = { filelistsBots: bots, filelistsFilter: c.filter, filelistsSource: c.open,
    filelistsRevealEmpty: c.reveal, filelistsExcluded: c.excluded };
  const rows = c.sidebar.map((s) => rowEl(s[0], s[1]));
  const api = build(code, state, rows);
  api.applyFilterHighlight(c.payload);
  return { classes: rows.map((r) => Array.from(r.cls).sort().join(" ")),
           hidden: api.reveal.hidden, enumerations: enumerations, rows: rows.length };
}

const out = { cases: 0, highlightMismatches: [], lookupMismatches: [], filteredAway: 0,
              enumerations: [], oldEnumerations: [], freshArrays: true };
for (let n = 0; n < 300; n++) {
  const c = makeCase();
  out.cases++;
  const a = runHighlight(current, c, false), b = runHighlight(old, c, false);
  if (JSON.stringify(a) !== JSON.stringify(b)) { out.highlightMismatches.push({ n: n, now: a, was: b }); }
  out.filteredAway += a.classes.filter((k) => k.indexOf("is-filtered-away") >= 0).length;
  const stateNow = { filelistsBots: c.bots }, stateOld = { filelistsBots: c.bots };
  const now = build(current, stateNow, []), was = build(old, stateOld, []);
  const asks = c.sidebar.map((s) => s[1]).concat(c.sidebar.map((s) => s[0].toUpperCase()),
    ["", "nobody", "constructor", "__proto__", "toString"]);
  asks.forEach((nick) => {
    const x = now.entriesForNick(nick), y = was.entriesForNick(nick);
    if (x.length !== y.length || x.some((row, i) => row !== y[i])) {
      out.lookupMismatches.push({ n: n, nick: nick });
    }
    // Two answers from ONE shared map must still be two arrays: a caller
    // that edits what it got must not edit what the next row is told.
    const oneMap = now.entriesByNick();
    const first = now.entriesForNick(nick, oneMap);
    first.push("scribble");
    if (now.entriesForNick(nick, oneMap).indexOf("scribble") >= 0) { out.freshArrays = false; }
  });
  if (n < 5) {
    const counted = runHighlight(current, Object.assign({}, c, { filter: "x" }), true);
    out.enumerations.push([counted.rows, counted.enumerations]);
    const counted0 = runHighlight(old, Object.assign({}, c, { filter: "x" }), true);
    out.oldEnumerations.push([counted0.rows, counted0.enumerations]);
  }
}
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class TheRealFunctionsUnderNode(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=120)
        finally:
            os.unlink(path)
        if done.returncode != 0:
            raise AssertionError(done.stderr.decode("utf-8", "replace"))
        cls.seen = json.loads(done.stdout.decode("utf-8"))

    def test_the_sidebar_classes_are_what_the_old_scan_gave(self):
        self.assertEqual(self.seen["cases"], 300)
        self.assertEqual(self.seen["highlightMismatches"], [])
        # The cases have to exercise the hiding, or matching proves nothing.
        self.assertGreater(self.seen["filteredAway"], 500)

    def test_a_single_lookup_answers_what_the_old_scan_answered(self):
        self.assertEqual(self.seen["lookupMismatches"], [])

    def test_each_lookup_hands_back_its_own_array(self):
        """The old scan built a new array every call; a caller that filters or
        sorts what it got must not be editing the map's own list."""
        self.assertTrue(self.seen["freshArrays"])

    def test_one_call_enumerates_the_bots_once_not_once_per_row(self):
        for rows, enumerations in self.seen["enumerations"]:
            self.assertEqual(enumerations, 1, "%d rows" % rows)
        # The harness can see the difference: the old scan enumerated per row.
        for rows, enumerations in self.seen["oldEnumerations"]:
            self.assertEqual(enumerations, rows)
        self.assertTrue(any(rows > 1 for rows, _ in self.seen["oldEnumerations"]))


class TheSourceSaysSo(unittest.TestCase):
    """The statements that carry the fix, for a runner without node."""

    def setUp(self):
        self.js = app_js()

    def test_the_highlight_builds_the_map_once_before_the_row_loop(self):
        body = function_body(self.js, "applyFilterHighlight")
        build = body.index("var byNick = entriesByNick();")
        loop = body.index("for (var i = 0; i < rows.length; i++) {")
        self.assertLess(build, loop)
        self.assertEqual(body.count("entriesByNick()"), 1)
        self.assertIn("var group = entriesForNick(nick, byNick);", body)

    def test_the_map_is_not_kept_between_calls(self):
        """entriesForNick()'s comment rules out a second map kept in step by
        hand; the one map lives in a local of the call that built it."""
        self.assertNotIn("state.filelistsByNick", self.js)
        self.assertNotIn("state.byNick", self.js)

    def test_a_lookup_without_a_map_builds_one_from_the_live_rows(self):
        body = function_body(self.js, "entriesForNick")
        self.assertIn('var group = (byNick || entriesByNick())[String(nick || "").toLowerCase()];', body)
        self.assertIn("return group ? group.slice() : [];", body)

    def test_the_map_is_keyed_as_the_old_scan_compared(self):
        body = function_body(self.js, "entriesByNick")
        self.assertIn("var byNick = Object.create(null);", body)
        self.assertIn('var nickLower = String(row.nick || row.bot || "").toLowerCase();', body)
        self.assertIn("(byNick[nickLower] || (byNick[nickLower] = [])).push(row);", body)


if __name__ == "__main__":
    unittest.main()
