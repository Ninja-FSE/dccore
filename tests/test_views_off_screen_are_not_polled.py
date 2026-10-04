"""The Downloads and List Browser polls run only while their view is on screen.

#1142: every open dashboard tab asked for /api/fetch/status (hundreds of KB
with a full fetch history) and /api/filelists/bots (a row per known bot, the
channel nick set built under channel_users_lock) every four seconds, on every
view, and rebuilt the Downloads tables and the List Browser sidebar from
them - though nothing outside #view-download draws the first, and nothing
outside #view-filelists reads the second. Both views already fetch fresh on
the way in (activateView), so the background ticks bought nothing the
operator could see.

Now each tick asks only while its own view is active and the browser tab is
not hidden. What has to stay as it was, and is pinned here too:
- the way in still loads: activateView("download") fetches the downloads and
  activateView("filelists") polls the sidebar, so a transfer or list that
  finished elsewhere is there the moment the view opens;
- the parts of the page visible on EVERY view - the sidebar status card's
  slots and queued counters, the notice badge, the Messages count, the
  connection dot - keep their own unconditional ticks (/api/queue, notices,
  messages, the console log), which never depended on these two polls;
- the console log keeps polling on every view: its comment records that
  choice, and the audit's skeptic said not to fold it in.
The node half runs the real interval callbacks and activateView() out of
app.js; it skips where node is missing (CI's runners have it), so the
statements are also pinned by reading the source.
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


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const NL = String.fromCharCode(10);

// The whole setInterval(...) statement whose body contains `marker`.
function intervalWith(marker) {
  const at = src.indexOf(marker);
  if (at < 0) { throw new Error("missing " + marker); }
  const start = src.lastIndexOf("  setInterval(", at);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") { depth++; }
    else if (src[i] === "}") { depth--; if (depth === 0) { break; } }
  }
  const end = src.indexOf(");", i);
  return src.slice(start, end + 2);
}
function fn(name) {
  const i = src.indexOf("  function " + name + "(");
  const j = src.indexOf(NL + "  }" + NL, i);
  if (i < 0 || j < 0) { throw new Error("missing " + name); }
  return src.slice(i, j + 4);
}

const pieces = {
  downloads: intervalWith("DOWNLOADS_POLL_MS);"),
  filelists: intervalWith("FILELISTS_BOTS_POLL_MS);"),
  queue: intervalWith('fetchJson("/api/queue").then(function (rows) {' + NL + "      markConnection(true);" + NL + "      renderSidebarStatus(rows);"),
};
const VIEWS = ["search", "download", "filelists", "tools", "live", "stats",
               "messages", "notices", "settings", "console"];

function harness(code) {
  const calls = {};
  const count = (name) => function () { calls[name] = (calls[name] || 0) + 1;
    return { then: function () { return { catch: function () {} }; } }; };
  const timers = [];
  const env = {
    state: { active: "search" },
    document: { hidden: false, getElementById: () => null },
    setInterval: (cb, ms) => { timers.push(cb); return timers.length; },
    loadDownloads: count("downloads"), pollFilelistsBots: count("filelists"),
    loadFilelists: count("loadFilelists"), loadSettings: count("settings"),
    loadStats: count("stats"), loadLive: count("live"), loadQueue: count("loadQueue"),
    loadRecord: count("record"), loadUpdateListSchedule: count("tools"),
    loadNotices: count("notices"), loadMessages: count("messages"),
    fetchJson: function (url) { calls["fetch " + url] = (calls["fetch " + url] || 0) + 1;
      return { then: function () { return { catch: function () {} }; } }; },
    markConnection: count("conn"), renderSidebarStatus: count("sidebar"),
    renderQueueStats: count("qs"), renderQueueTable: count("qt"),
    DOWNLOADS_POLL_MS: 4000, FILELISTS_BOTS_POLL_MS: 4000, REFRESH_MS: 8000,
    views: {}, el: { navItems: [], pageTitle: {}, pageSub: {} }, t: (k) => k,
  };
  VIEWS.forEach((v) => { env.views[v] = { title: v, sub: v }; });
  const names = Object.keys(env);
  const run = new Function(...names, code + NL +
    "return typeof activateView === 'function' ? activateView : null;");
  const activate = run(...names.map((n) => env[n]));
  return { env: env, calls: calls, timers: timers, activate: activate };
}

const out = { ticks: {}, entry: {}, queue: {} };
["downloads", "filelists"].forEach((which) => {
  const h = harness(pieces[which]);
  if (h.timers.length !== 1) { throw new Error(which + ": expected one timer"); }
  out.ticks[which] = {};
  VIEWS.forEach((view) => {
    [false, true].forEach((hidden) => {
      h.env.state.active = view;
      h.env.document.hidden = hidden;
      const before = h.calls[which] || 0;
      h.timers[0]();
      out.ticks[which][view + (hidden ? "/hidden" : "")] = (h.calls[which] || 0) - before;
    });
  });
});

const q = harness(pieces.queue);
VIEWS.forEach((view) => {
  q.env.state.active = view;
  q.env.document.hidden = false;
  const before = q.calls["fetch /api/queue"] || 0;
  const notices = q.calls.notices || 0, messages = q.calls.messages || 0;
  q.timers[0]();
  out.queue[view] = [(q.calls["fetch /api/queue"] || 0) - before,
                     (q.calls.notices || 0) - notices, (q.calls.messages || 0) - messages];
});

const a = harness(fn("activateView"));
VIEWS.forEach((view) => {
  const before = Object.assign({}, a.calls);
  a.activate(view);
  out.entry[view] = { downloads: (a.calls.downloads || 0) - (before.downloads || 0),
                      filelists: (a.calls.filelists || 0) - (before.filelists || 0) };
});
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class TheRealTicksUnderNode(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        if done.returncode != 0:
            raise AssertionError(done.stderr.decode("utf-8", "replace"))
        cls.seen = json.loads(done.stdout.decode("utf-8"))

    def only_on(self, which, view):
        ticks = self.seen["ticks"][which]
        expected = {key: 0 for key in ticks}
        expected[view] = 1
        self.assertEqual(ticks, expected)

    def test_the_downloads_tick_asks_only_while_downloads_is_on_screen(self):
        self.only_on("downloads", "download")

    def test_the_sidebar_tick_asks_only_while_the_list_browser_is_on_screen(self):
        self.only_on("filelists", "filelists")

    def test_opening_either_view_still_loads_it_at_once(self):
        entry = self.seen["entry"]
        self.assertEqual(entry["download"]["downloads"], 1)
        self.assertEqual(entry["filelists"]["filelists"], 1)
        for view, calls in entry.items():
            if view != "download":
                self.assertEqual(calls["downloads"], 0, view)
            if view != "filelists":
                self.assertEqual(calls["filelists"], 0, view)

    def test_the_always_visible_counters_are_still_fed_on_every_view(self):
        """Slots, queued, the notice badge and the Messages count come from
        this tick, not from the two that now stop off-screen."""
        for view, (queue, notices, messages) in self.seen["queue"].items():
            self.assertEqual((queue, notices, messages), (1, 1, 1), view)


class TheSourceSaysSo(unittest.TestCase):
    """The statements that carry the change, for a runner without node."""

    def setUp(self):
        self.js = read("web", "app.js")

    def test_each_tick_is_gated_on_its_own_view(self):
        self.assertIn(
            '    if (state.active === "download" && !document.hidden) { loadDownloads(); }\n'
            "  }, DOWNLOADS_POLL_MS);", self.js)
        self.assertIn(
            '    if (state.active === "filelists" && !document.hidden) { pollFilelistsBots(); }\n'
            "  }, FILELISTS_BOTS_POLL_MS);", self.js)

    def test_neither_is_polled_unconditionally_any_more(self):
        self.assertNotIn("setInterval(loadDownloads, DOWNLOADS_POLL_MS);", self.js)
        self.assertNotIn("setInterval(pollFilelistsBots, FILELISTS_BOTS_POLL_MS);", self.js)

    def test_the_way_in_still_loads_both(self):
        body = self.js.split("  function activateView(name) {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn('    if (name === "download") { loadDownloads(); }', body)
        self.assertRegex(body, r'if \(name === "filelists"\) \{\n\s+pollFilelistsBots\(\);')

    def test_the_console_log_still_polls_on_every_view(self):
        self.assertIn("  consoleLogTimer = setInterval(pollConsoleLog, CONSOLE_LOG_POLL_MS);\n", self.js)


class WhatTheyDrawIsInsideTheirView(unittest.TestCase):
    """Why gating is safe: what each poll renders sits inside its own view's
    section, so a view that is not active has nothing on screen to update."""

    def section(self, html, view):
        start = html.index('id="view-%s"' % view)
        end = html.index("</section>", start)
        return html[start:end]

    def test_the_downloads_output_is_inside_the_downloads_view(self):
        html = read("web", "index.html")
        inside = self.section(html, "download")
        for node in ("downloads-summary", "downloads-boxes"):
            self.assertIn('id="%s"' % node, inside)
            self.assertEqual(html.count('id="%s"' % node), 1)

    def test_the_sidebar_output_is_inside_the_list_browser_view(self):
        html = read("web", "index.html")
        inside = self.section(html, "filelists")
        for node in ("filelists-bot-list", "filelists-freshness"):
            self.assertIn('id="%s"' % node, inside)
            self.assertEqual(html.count('id="%s"' % node), 1)

    def test_nothing_on_the_always_visible_sidebar_comes_from_either_poll(self):
        """The left sidebar (nav, status card) is outside every view; no id
        there is one the two renderers write to."""
        html = read("web", "index.html")
        aside = html[html.index("<aside"):html.index("</aside>")]
        for node in ("downloads-summary", "downloads-boxes", "filelists-bot-list",
                     "filelists-freshness"):
            self.assertNotIn('id="%s"' % node, aside)
        self.assertFalse(re.search(r'id="(downloads|filelists)-', aside))


if __name__ == "__main__":
    unittest.main()
