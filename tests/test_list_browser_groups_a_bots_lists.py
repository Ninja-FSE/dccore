"""#399: a bot's several lists share one sidebar row, with tabs above the
table to switch between them, instead of "SomeBot" and "SomeBot - rar"
reading as two unrelated bots.

Reported live, with a screenshot: an operator's sidebar carried a second row
for every bot that happens to publish a RAR or VIDEO list alongside its main
one - two different real bots each shown twice under oddly similar names -
which reads as pairs of bots sharing similar names rather than one bot each
with two lists.

Structural, like every other JS check in this suite - nothing here executes
JavaScript, so these assert that the expected wiring is written into app.js
and style.css rather than exercising it. Flagged during design, on the issue
itself, as the riskiest part of this feature: the sidebar's `.bot-row`
elements are walked in five different places - click routing, focus
restoration, keyboard navigation, and two kinds of filtering - and a grouped
row that keeps working "mostly by accident" in one of them is how a filter's
"Show N with no match" count goes quietly wrong rather than visibly broken.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                 encoding="utf-8") as handle:
        return handle.read()


def index_html():
    with io.open(os.path.join(REPO_ROOT, "web", "index.html"),
                 encoding="utf-8") as handle:
        return handle.read()


def style_css():
    with io.open(os.path.join(REPO_ROOT, "web", "style.css"),
                 encoding="utf-8") as handle:
        return handle.read()


def function_body(source, name):
    """The text of one top-level function in app.js. They are all written at
    two-space indentation, so the first closing brace at that indent after
    the signature is the function's own."""
    return source.split("function " + name + "(", 1)[1].split("\n  }", 1)[0]


class TheSidebarGroupsByNick(unittest.TestCase):
    """renderFilelistsSwitcher() buckets the flat rows /api/filelists/bots
    returns before drawing anything, rather than drawing one row per row."""

    def body(self):
        return function_body(app_js(), "renderFilelistsSwitcher")

    def test_rows_are_bucketed_by_nick(self):
        body = self.body()
        self.assertIn("groupsByNick", body)
        self.assertIn("row.nick || row.bot", body)

    def test_one_row_is_appended_per_group_not_per_flat_row(self):
        """The defect this whole issue is about: a naive port draws
        list.appendChild(botRow(row)) inside the rows.forEach loop, one DOM
        row per flat entry."""
        body = self.body()
        self.assertIn("list.appendChild(botRow(groupsByNick[nickKey]))", body)
        self.assertNotIn("list.appendChild(botRow(row))", body)


class TheCollapsedRowSpeaksForItsGroup(unittest.TestCase):
    """botRow() takes a {nick, entries} group, not a single flat row."""

    def body(self):
        return function_body(app_js(), "botRow")

    def test_the_primary_entry_is_used_for_the_row_s_own_signals(self):
        body = self.body()
        self.assertIn("primaryEntry(group)", body)
        self.assertIn('button.dataset.bot = primary.bot;', body)
        self.assertIn('button.dataset.nick = group.nick;', body)

    def test_a_grouped_row_shows_the_bare_nick_not_a_per_list_label(self):
        """The concrete defect: "SomeBot - rar" as a name is a per-LIST label
        wearing a per-BOT row - it names one of the lists the row now
        represents, and reads as a different bot from "SomeBot" beside it."""
        self.assertIn(
            'name.textContent = grouped ? group.nick : (primary.label || primary.bot);',
            self.body())

    def test_an_ungrouped_row_keeps_its_existing_label(self):
        """Own lists, and the ordinary single-list fetched bot, must look
        exactly as they did before this feature - "Our own list", a served
        list's own name, or the bare nick, never overridden by group.nick
        merely because grouping now exists."""
        body = self.body()
        self.assertIn("primary.label || primary.bot", body)

    def test_the_row_carries_how_many_lists_it_groups(self):
        """Some visible sign a row has more behind it, or the tabs above the
        table are the only way to discover a bot has a second list at all."""
        body = self.body()
        self.assertIn("grouped", body)
        self.assertIn("bot-row-lists-badge", body)

    def test_the_count_shown_is_the_primary_s_not_a_sum(self):
        """A RAR list packs albums the main list already counts - adding the
        two would not be a real total, so this must read one count, not
        reduce across the group."""
        body = self.body()
        self.assertIn("primary.count", body)
        self.assertNotIn("reduce(", body)


class TheTabsReadTheMarkerDirectly(unittest.TestCase):
    """renderFilelistsTabs(): one tab per list the OPEN bot has, labelled by
    its own marker - not the sidebar's "label" field, which was built for a
    single collapsed row and mixes two different conventions (a fetched
    bot's "bot - marker" against an own list's own name)."""

    def body(self):
        return function_body(app_js(), "renderFilelistsTabs")

    def test_hidden_for_a_bot_with_only_one_list(self):
        """Every own list, and the ordinary fetched bot - a tab bar of one
        tab is not a choice, it is a bar that should not be there."""
        body = self.body()
        self.assertIn("entries.length < 2", body)
        self.assertIn("container.hidden = true", body)

    def test_the_label_comes_from_list_not_from_label(self):
        body = self.body()
        self.assertIn("entry.list", body)
        self.assertNotIn("entry.label", body)

    def test_the_bare_list_reads_as_main_not_as_nothing(self):
        """A tab with no text is not a tab an operator can click on
        purpose."""
        self.assertIn('"Main"', self.body())

    def test_clicking_a_tab_switches_the_open_source(self):
        click_handler = app_js().split("el.filelistsListTabs.addEventListener", 1)[1]
        self.assertIn("state.filelistsSource = tab.dataset.bot;", click_handler[:600])


class GroupMembershipReplacesExactKeyMatching(unittest.TestCase):
    """The riskiest part, per the design discussion on #399: five places
    walk .bot-row, and a grouped row answering for several list-keys at once
    has to be recognised as "the open one", "the excluded one" and "the one
    with a match" by NICK - comparing its dataset.bot (always its PRIMARY
    list's key) against an exact source key would silently stop matching the
    moment a non-primary tab is open."""

    def source(self):
        return app_js()

    def test_active_row_highlighting_compares_by_nick(self):
        body = function_body(self.source(), "markFilelistsActiveBot")
        self.assertIn("nickOfSource(state.filelistsSource)", body)
        self.assertIn('rows[i].dataset.nick', body)
        self.assertNotIn('rows[i].dataset.bot === state.filelistsSource', body)

    def test_the_sidebar_click_reopens_the_bot_s_last_list_not_always_primary(self):
        """Re-clicking a row whose RAR tab is already open must not silently
        reset the table to the main list on every redraw."""
        body = self.source().split('el.filelistsBotList.addEventListener("click"', 1)[1][:2200]
        self.assertIn("nickOfSource(row.dataset.bot) === nickOfSource(state.filelistsSource)", body)

    def test_the_exclude_while_filtering_toggle_is_keyed_by_nick(self):
        body = self.source().split('el.filelistsBotList.addEventListener("click"', 1)[1][:2200]
        self.assertIn("row.dataset.nick || row.dataset.bot", body)

    def test_the_empty_check_considers_every_list_in_the_group(self):
        body = function_body(self.source(), "applyFilterHighlight")
        self.assertIn("entriesForNick(nick)", body)
        self.assertIn(".every(", body)

    def test_the_pinned_check_is_by_nick(self):
        body = function_body(self.source(), "applyFilterHighlight")
        self.assertIn("nickOfSource(state.filelistsSource)", body)

    def test_folder_group_visibility_resolves_the_nick_first(self):
        body = function_body(self.source(), "visibleFilterGroups")
        self.assertIn("nickOfSource(group.bot)", body)

    def test_show_none_excludes_by_nick_too(self):
        body = function_body(self.source(), "setEveryListShown")
        self.assertIn("nickOfSource(name)", body)


class NickOfSourceHandlesBothKindsOfKey(unittest.TestCase):
    """One helper, reused everywhere a source key needs reducing to the bot
    it belongs to - own ("__own__:video") or fetched ("d_f_d/rar")."""

    def test_it_is_defined_once(self):
        self.assertEqual(app_js().count("function nickOfSource("), 1)

    def test_it_delegates_to_the_existing_splitter(self):
        body = function_body(app_js(), "nickOfSource")
        self.assertIn("splitFetchedSource(source).nick", body)


class ThePurgeButtonNamesWhatItActuallyRemoves(unittest.TestCase):
    """#389 already made a purge remove the whole bot's every list, resolving
    <nick>/<marker> to its bot - the button's wording was written for an
    older UI where a row WAS a single list, and #399's grouping makes the
    mismatch obvious rather than a technicality."""

    def test_the_button_text_names_the_bot_not_a_list(self):
        html = index_html()
        button_html = html.split('id="filelists-purge-btn"', 1)[1].split("</button>", 1)[0]
        self.assertIn("Purge this bot", button_html)
        self.assertNotIn(">Purge this list<", html)

    def test_the_confirm_dialog_names_the_bot_not_the_open_tab_s_label(self):
        """row.label could be "SomeBot - rar" if that happens to be the list
        open when Purge is pressed - confirming a bot-wide delete with a
        single list's name understates what is about to happen."""
        body = function_body(app_js(), "purgeCurrentList")
        self.assertIn("row.nick || row.label || row.bot || source", body)


class TheTabBarIsDeclaredAndHiddenCorrectly(unittest.TestCase):
    """Covers the same ground test_web_assets.py's generic guards do, kept
    here too because it is specific, readable evidence that this feature's
    one new element is real: declared, referenced, and paired with a
    [hidden] rule rather than the display:flex silently winning."""

    def test_the_container_exists_in_the_page(self):
        self.assertIn('id="filelists-list-tabs"', index_html())

    def test_the_element_map_reads_it(self):
        self.assertIn(
            'filelistsListTabs: document.getElementById("filelists-list-tabs")',
            app_js())

    def test_hidden_is_paired_with_a_display_none_rule(self):
        css = style_css()
        self.assertIn(".filelists-list-tabs[hidden]", css)
        pair = css.split(".filelists-list-tabs[hidden]", 1)[1].split("}", 1)[0]
        self.assertIn("display: none", pair)


if __name__ == "__main__":
    unittest.main()
