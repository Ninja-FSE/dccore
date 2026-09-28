"""#376, part 1: one bot seen under two nicks is one List Browser row.

Two ways, both decided on #376 and both display only - fetched_bot_lists,
known_bots and the counters stay keyed per nick:

- A NICK message from a known bot is proof: its rows merge under the new nick.
- Option B, by ident: same ident, same advertised file count, the old nick's
  departure OBSERVED (not just absent), and never advertising at the same
  time. The ident is held in memory only - never written, never logged -
  and no host or IP is kept at all.
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

import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

T0 = 1_000_000.0
NL = chr(10)


class Case(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        config.channel_users["#chan"] = set()
        original = dict(runtime.known_bots)
        runtime.known_bots.clear()
        self.addCleanup(lambda: (runtime.known_bots.clear(), runtime.known_bots.update(original)))

    def advertise(self, nick, at, files=5000, ident="packbot", online=True):
        """A bot's advert arriving at `at`, from nick!ident@host."""
        key = nick.lower()
        entry = dict(runtime.known_bots.get(key) or {})
        entry.update({"nick": nick, "files": files, "last_seen": at})
        runtime.known_bots[key] = entry
        irc._capture_bot_ident(nick, f"{ident}@host-{nick}.example.net", now=at)
        if online:
            config.channel_users["#chan"].add(key)

    def leave(self, nick, at):
        """An observed QUIT/PART - what the handlers do."""
        config.channel_users["#chan"].discard(nick.lower())
        irc.note_observed_departure(nick.lower(), "#chan", now=at)

    def merges(self):
        return webserver._ident_merges(dict(runtime.known_bots), webserver.present_nicks())


class ByIdent(Case):
    def the_usual_reconnect(self):
        """PackBot advertises, its connection dies; it comes back as PackBot_
        while the ghost still sits in the channel, then the ghost times out."""
        self.advertise("PackBot", T0)
        self.advertise("PackBot_", T0 + 300)
        self.leave("PackBot", T0 + 400)

    def test_the_usual_reconnect_merges(self):
        self.the_usual_reconnect()
        self.assertEqual(self.merges(), {"packbot": "PackBot_"})

    def test_a_different_ident_does_not(self):
        self.advertise("PackBot", T0)
        self.advertise("PackBot_", T0 + 300, ident="someoneelse")
        self.leave("PackBot", T0 + 400)
        self.assertEqual(self.merges(), {})

    def test_a_different_file_count_does_not(self):
        self.advertise("PackBot", T0, files=5000)
        self.advertise("PackBot_", T0 + 300, files=5001)
        self.leave("PackBot", T0 + 400)
        self.assertEqual(self.merges(), {})

    def test_absence_alone_is_not_a_departure(self):
        """Gone from the channel without a QUIT/PART we saw: it may never
        have been in a channel we share."""
        self.advertise("PackBot", T0)
        self.advertise("PackBot_", T0 + 300)
        config.channel_users["#chan"].discard("packbot")
        self.assertEqual(self.merges(), {})

    def test_back_again_is_not_merged(self):
        self.the_usual_reconnect()
        config.channel_users["#chan"].add("packbot")
        self.assertEqual(self.merges(), {})

    def test_two_bots_advertising_at_the_same_time_are_two_bots(self):
        """Same ident, same count, but both were alive at once: the old
        nick's last advert came after the new nick's first."""
        self.advertise("PackBot", T0)
        self.advertise("PackBot_", T0 + 300)
        self.advertise("PackBot", T0 + 350)
        self.leave("PackBot", T0 + 400)
        self.assertEqual(self.merges(), {})

    def test_two_candidates_are_not_an_answer(self):
        self.advertise("PackBot", T0)
        self.advertise("PackBot_", T0 + 300)
        self.advertise("PackBot__", T0 + 310)
        self.leave("PackBot", T0 + 400)
        self.assertEqual(self.merges(), {})

    def test_hours_apart_is_a_coincidence(self):
        """The review's reproduction: the old nick quits, and three hours later a
        different bot with the same ident and file count appears."""
        self.advertise("PackBot", T0)
        self.leave("PackBot", T0 + 60)
        self.advertise("OtherBot", T0 + 3 * 3600)
        self.assertEqual(self.merges(), {})

    def test_inside_the_window(self):
        window = irc.IDENT_MERGE_WINDOW_SECONDS
        # A clean QUIT first, the new nick a little later.
        self.advertise("PackBot", T0)
        self.leave("PackBot", T0 + 60)
        self.advertise("PackBot_", T0 + 60 + window - 1)
        self.assertEqual(self.merges(), {"packbot": "PackBot_"})

    def test_just_outside_the_window(self):
        window = irc.IDENT_MERGE_WINDOW_SECONDS
        self.advertise("PackBot", T0)
        self.leave("PackBot", T0 + 60)
        self.advertise("PackBot_", T0 + 60 + window + 1)
        self.assertEqual(self.merges(), {})

    def test_the_ghost_leaving_after_the_new_nick_came_is_in_the_window_too(self):
        window = irc.IDENT_MERGE_WINDOW_SECONDS
        self.advertise("PackBot", T0)
        self.advertise("PackBot_", T0 + 100)
        self.leave("PackBot", T0 + 100 + window + 1)
        self.assertEqual(self.merges(), {}, "too long after the new nick appeared")

    def test_first_seen_is_the_join_when_we_saw_one(self):
        """A bot's first advert can come long after it joined: the join is
        when it appeared, so a slow advert does not push it out of the
        window."""
        window = irc.IDENT_MERGE_WINDOW_SECONDS
        self.advertise("PackBot", T0 - 100)
        self.leave("PackBot", T0)
        irc.note_join_seen("PackBot_", now=T0 + 10)
        # First advert just inside the window from the JOIN, and past it from
        # the departure: measured from the advert this would not merge.
        self.advertise("PackBot_", T0 + 10 + window - 5)
        self.assertEqual(runtime.bot_idents["packbot_"]["first_seen"], T0 + 10)
        self.assertEqual(self.merges(), {"packbot": "PackBot_"})

    def test_a_join_is_forgotten_after_the_window(self):
        irc.note_join_seen("SomeUser", now=T0)
        irc.note_join_seen("OtherUser", now=T0 + irc.IDENT_MERGE_WINDOW_SECONDS + 1)
        self.assertEqual(set(runtime.recent_joins), {"otheruser"})

    def test_only_a_known_bot_ident_is_kept(self):
        irc._capture_bot_ident("SomeUser", "someuser@host.example.net", now=T0)
        self.assertNotIn("someuser", runtime.bot_idents)

    def test_a_changed_ident_is_a_new_connection(self):
        self.advertise("PackBot", T0)
        self.advertise("PackBot", T0 + 60, ident="other")
        self.assertEqual(runtime.bot_idents["packbot"], {"ident": "other", "first_seen": T0 + 60})

    def test_a_departure_is_recorded_only_for_a_bot_with_an_ident(self):
        irc.note_observed_departure("someuser", "#chan", now=T0)
        self.assertNotIn("someuser", runtime.bot_departures)

    def test_the_sidebar_shows_one_row_under_the_current_nick(self):
        self.the_usual_reconnect()
        self.set_config(fetched_bot_lists={"packbot": {
            "bot": "PackBot", "fetched_at": T0, "entry_count": 10,
            "advert_when_fetched": {"files": 5000}}})
        rows = webserver.build_fetched_bot_list_summaries()
        mine = [r for r in rows if str(r.get("bot", "")).lower().startswith("packbot")]
        self.assertEqual(sorted(r["bot"] for r in mine), ["PackBot", "PackBot_"],
                         "display only: each row keeps its real nick")
        self.assertEqual({r["nick"] for r in mine}, {"PackBot_"})


class MemoryOnly(Case):
    def test_no_host_is_kept_in_any_form(self):
        self.advertise("PackBot", T0)
        self.assertEqual(runtime.bot_idents["packbot"], {"ident": "packbot", "first_seen": T0})
        self.assertNotIn("ident", runtime.known_bots["packbot"], "the registry is saved to disk")

    def test_nothing_that_writes_to_disk_reads_it(self):
        for name in ("db.py", "irc.py", "announce.py"):
            with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                code = handle.read()
            for line in code.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if name != "irc.py":
                    self.assertNotIn("bot_idents", stripped, f"{name}: {stripped}")
                else:
                    # irc.py writes it; it must never print it.
                    if "bot_idents" in stripped:
                        self.assertNotIn("print(", stripped)


class ByNickMessage(Case):
    def test_a_known_bot_changing_nick_is_one_row_under_the_new_one(self):
        self.advertise("PackBot_", T0)
        self.assertTrue(irc.note_bot_renamed("PackBot_", "PackBot"))
        self.assertEqual(runtime.resolve_display_nick("PackBot_"), "PackBot")
        self.assertEqual(runtime.resolve_display_nick("PackBot"), "PackBot")

    def test_a_user_who_is_not_a_bot_is_left_alone(self):
        self.assertFalse(irc.note_bot_renamed("SomeUser", "SomeUser_away"))
        self.assertEqual(runtime.nick_aliases, {})

    def test_back_and_forth_never_aliases_a_nick_to_itself(self):
        self.advertise("PackBot", T0)
        irc.note_bot_renamed("PackBot", "PackBot_")
        self.advertise("PackBot_", T0 + 1)
        irc.note_bot_renamed("PackBot_", "PackBot")
        self.assertEqual(runtime.resolve_display_nick("PackBot"), "PackBot")
        self.assertEqual(runtime.resolve_display_nick("PackBot_"), "PackBot")

    def test_an_older_alias_follows_the_rename(self):
        self.advertise("PackBot", T0)
        runtime.nick_aliases["packbot__"] = "PackBot"
        irc.note_bot_renamed("PackBot", "PackBot_")
        self.assertEqual(runtime.resolve_display_nick("PackBot__"), "PackBot_")

    def test_the_new_nick_stops_being_an_alias_of_another_bot(self):
        """PackBot_ was once aliased to some other bot; now PackBot is
        PackBot_, and its row must not be filed under that other bot."""
        self.advertise("PackBot", T0)
        runtime.nick_aliases["packbot_"] = "OtherBot"
        irc.note_bot_renamed("PackBot", "PackBot_")
        self.assertEqual(runtime.resolve_display_nick("PackBot_"), "PackBot_")
        self.assertEqual(runtime.resolve_display_nick("PackBot"), "PackBot_")

    def test_someone_else_on_the_bot_s_nick_is_not_merged(self):
        """The review's case: the bot is away, somebody takes its nick and renames.
        We hold the bot's ident, and the NICK line's is different."""
        self.advertise("PackBot", T0, ident="packbot")
        self.assertFalse(irc.note_bot_renamed("PackBot", "Zed", "someoneelse"))
        self.assertEqual(runtime.resolve_display_nick("PackBot"), "PackBot")

    def test_the_same_ident_on_the_nick_line_merges(self):
        self.advertise("PackBot_", T0, ident="packbot")
        self.assertTrue(irc.note_bot_renamed("PackBot_", "PackBot", "packbot"))

    def test_no_ident_held_is_still_proof(self):
        self.set_config(fetched_bot_lists={"quietbot": {"bot": "QuietBot"}})
        self.assertTrue(irc.note_bot_renamed("QuietBot", "QuietBot_", "anything"))

    def test_a_held_list_counts_as_known(self):
        self.set_config(fetched_bot_lists={"quietbot": {"bot": "QuietBot"}})
        self.assertTrue(irc.note_bot_renamed("QuietBot", "QuietBot_"))


class TheWiring(unittest.TestCase):
    def read(self, *parts):
        with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_every_advert_line_passes_its_ident(self):
        code = self.read("irc.py")
        at = code.index("_capture_channel_advert(user, target_chan, msg)\n")
        self.assertIn("_capture_bot_ident(user, user_host)", code[at:at + 200])

    def test_the_nick_handler_merges_with_the_line_s_ident(self):
        code = self.read("irc.py")
        at = code.index("note_nick_change(nick_match.group(1),")
        handler = code[at:at + 700]
        self.assertIn("note_bot_renamed(nick_match.group(1),", handler)
        self.assertIn("renamed_ident.group(1) if renamed_ident else None", handler)

    def test_the_join_handler_notes_the_join(self):
        code = self.read("irc.py")
        at = code.index("note_possible_reconnect(joined_user)" + NL)
        self.assertIn("note_join_seen(joined_user)", code[at:at + 200])

    def test_the_summary_applies_it(self):
        code = self.read("webserver.py")
        self.assertIn("merges = _ident_merges(known, present)", code)
        self.assertEqual(code.count('"nick": _display_nick(bot, present, merges),'), 2)

    def test_the_page_opens_the_held_list_and_shows_the_bot_here(self):
        js = self.read("web", "app.js")
        start = js.index("function primaryEntry(group)")
        body = js[start:js.index("function otherNicks", start)]
        self.assertLess(body.index("group.entries[h].held"), body.index("return group.entries[0];"))
        self.assertIn("var online = groupOnline(group, primary);", js)
        self.assertIn('t("filelists.alsoSeenAs")', js)


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
const code = ["splitFetchedSource", "nickOfSource", "displayNickOfSource", "entriesForNick",
              "renderFilelistsTabs", "markFilelistsActiveBot"].map(fn).join(NL);
function node() {
  const cls = new Set(), attrs = {};
  return { dataset: {}, hidden: true, innerHTML: "", children: [],
    classList: { toggle: (c, on) => on ? cls.add(c) : cls.delete(c), has: (c) => cls.has(c) },
    setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: (k) => { delete attrs[k]; },
    appendChild(ch) { this.children.push(ch); }, attrs };
}
const document = { createElement: () => node() };
const rowEls = [node(), node()];
rowEls[0].dataset.nick = "PackBot_";
rowEls[1].dataset.nick = "OtherBot";
const el = { filelistsListTabs: node(),
             filelistsBotList: { querySelectorAll: () => rowEls } };
const t = (k) => k;
function run(bots, source) {
  const state = { filelistsBots: bots, filelistsSource: source };
  el.filelistsListTabs.children = [];
  new Function("state", "el", "document", "t", code + NL + "markFilelistsActiveBot();")(state, el, document, t);
  return { tabs: el.filelistsListTabs.hidden ? 0 : el.filelistsListTabs.children.length,
           active: rowEls.filter(r => r.classList.has("is-active")).map(r => r.dataset.nick).join(",") };
}
const merged = {
  "PackBot": { bot: "PackBot", nick: "PackBot_", list: "", held: true },
  "PackBot_": { bot: "PackBot_", nick: "PackBot_", list: "", held: false }
};
const mergedTwoLists = Object.assign({}, merged, {
  "PackBot/rar": { bot: "PackBot/rar", nick: "PackBot_", list: "rar", held: true }
});
const out = {};
let r = run(merged, "PackBot");
out.oneListTabs = r.tabs; out.oneListActive = r.active;
r = run(mergedTwoLists, "PackBot");
out.twoListsTabs = r.tabs; out.twoListsActive = r.active;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class ThePageOnAMergedRow(unittest.TestCase):
    """The review's follow-up, run the way it was found: the real functions out of
    app.js under node, on a merged bot's rows - a held list under the old
    nick, an advert-only entry under the new, both shown as the new."""

    def run_page(self):
        import json
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        return json.loads(done.stdout.decode("utf-8"))

    def test_the_merged_row_is_marked_open(self):
        seen = self.run_page()
        self.assertEqual(seen["oneListActive"], "PackBot_")
        self.assertEqual(seen["twoListsActive"], "PackBot_")

    def test_tabs_are_the_lists_we_hold_not_the_nicks(self):
        seen = self.run_page()
        self.assertEqual(seen["oneListTabs"], 0, "one list held: no tab bar")
        self.assertEqual(seen["twoListsTabs"], 2, "two lists held: both tabs")

    def test_the_badge_counts_lists_not_entries(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        self.assertIn("var listCount = group.entries.filter(function (entry) { return entry.held; }).length;", js)
        self.assertIn("if (listCount > 1) {", js)
        self.assertNotIn("badge.textContent = String(group.entries.length);", js)


if __name__ == "__main__":
    unittest.main()
