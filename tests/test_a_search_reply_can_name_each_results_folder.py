"""A search reply can name each result's folder, on a line of its own (#1228).

An @find result named the file but not the folder it sits in, and in a ripped
collection that is often all that tells two hits apart: "05-some_song.flac"
from a studio album, a live album and a tribute differ only in their folder.
The folder cannot go on the result line itself - people paste those lines back
as requests, and AutoQ copies them verbatim - so SEARCH_SHOW_FOLDER sends it
on a "From:" line of its own above that folder's files.

The maintainer's conditions, each pinned below:
- off by default, and off is today's reply byte for byte;
- one From: line per distinct folder (results grouped and sorted by folder),
  never one per result, and MAX_SEARCH_RESULTS still counts files only;
- the From: line never starts with "!", and the result lines are unchanged,
  including the fit_irc_line() cut and its without_audio_info() fallback;
- the folder as the list shows it, cut from the LEFT past
  SEARCH_FOLDER_MAX_CHARS, and still fitted by fit_irc_line();
- coloured from the theme, so THEME=plain sends no colour code;
- no reply at all when nothing matches.
"""

import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import theme  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402
from tests.test_webserver import write_master_list  # noqa: E402

BS = chr(92)
NICK = "SomeBot"
USER = "someuser"
CHANNEL = "#somechannel"


def heading(*parts):
    """A folder heading as update_list.py writes it: prefix, parts, trailing backslash."""
    return list_mod.LIST_FOLDER_PREFIX + BS.join(parts) + BS


ZETA = heading("Rock", "Zeta Band", "2001 - Last Album")
ALPHA = heading("Rock", "Alpha Band", "1990 - First Album")
LIVE = heading("Live", "Zeta Band", "2003 - Live Somewhere")

# One list, in update_list.py's on-disk shape. ZETA appears twice, as a fetched
# or hand-edited list can have it: grouping must bring its files together, not
# just skip a repeated heading that happens to follow itself.
FOLDERS = [
    (None, [("00-some_song-loose.flac", "3.00MB")]),
    (ZETA, [("01-some_song.flac", "19.73MB"), ("02-other_tune.flac", "20.00MB")]),
    (ALPHA, [("05-some_song.flac", "32.04MB")]),
    (LIVE, [("10-some_song-live.mp3", "11.40MB 4m31s 320/44.1/JS")]),
    (ZETA, [("07-some_song-edit.flac", "9.10MB")]),
]


def reply_as_before(user, search_term, channel):
    """execute_search()'s reply before #1228, copied from origin/main cb7148e8.

    The header, then each capped row in list order through fit_irc_line(),
    falling back to without_audio_info() when the budget would cut the row.
    """
    wanted = library.list_name_for_request(channel)
    search_words = list_mod.split_search_term(list_mod.strip_control_codes(search_term))
    max_results = getattr(config, 'MAX_SEARCH_RESULTS', 5)
    if search_words:
        found_entries, total_matches = list_mod.find_matching_entries(
            search_words, limit=max_results, name=wanted)
    else:
        found_entries, total_matches = [], 0
    matches = [entry["line"] for entry in found_entries]
    queued = []
    if matches:
        oserve = sys.modules['oserve']
        before = len(oserve.queued)
        announce.send_search_result_header(user, search_term, total_matches, channel)
        queued.extend(message for _u, message, *_ in oserve.queued[before:])
        del oserve.queued[before:]
        BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()
        for match in matches:
            def _build(shown_match):
                block_match = (f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} "
                               f"{shown_match}{R} {BG_CYAN_BLOCK} {BG_RED_BLOCK} ")
                return f"PRIVMSG {user} :{block_match}\r\n"

            line = announce.fit_irc_line(_build, match)
            if line != _build(match) and list_mod.without_audio_info(match) != match:
                line = announce.fit_irc_line(_build, list_mod.without_audio_info(match))
            queued.append(line)
    return queued


class SearchCase(DCCoreTestCase):

    folders = FOLDERS

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        write_master_list(self.tree.lists, NICK, self.folders)
        self.set_config(FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME=NICK, NICKNAME=NICK, CHANNEL=CHANNEL,
                        MAX_SEARCH_RESULTS=10, THEME="classic",
                        search_inprogress=False, update_inprogress=False)

    def reply(self, term, **settings):
        if settings:
            self.set_config(**settings)
        self.oserve.queued.clear()
        list_mod.execute_search(RecordingSocket(), USER, term, CHANNEL)
        return [message for _u, message, *_ in self.oserve.queued]

    @staticmethod
    def text(line):
        """What the user is sent: the message after "PRIVMSG <user> :"."""
        prefix = f"PRIVMSG {USER} :"
        assert line.startswith(prefix), line
        return line[len(prefix):].rstrip("\r\n")

    @classmethod
    def label(cls, line):
        """A From: line as the user reads it: no colour codes, no frame spaces."""
        return list_mod.strip_control_codes(cls.text(line)).strip()

    def from_lines(self, lines):
        return [line for line in lines if "::INFO::" not in line and "Search Result:" not in line]

    def rows(self, lines):
        return [line for line in lines if "::INFO::" in line]


class OffIsTodaysReply(SearchCase):

    TERMS = ("some song", "some_song live", "tune", "02-other", "nothing-like-this")

    def test_the_setting_is_off_by_default(self):
        self.assertIs(config.SEARCH_SHOW_FOLDER, False)

    def test_off_is_byte_for_byte_the_old_reply(self):
        for term in self.TERMS:
            for cap in (1, 2, 10):
                with self.subTest(term=term, cap=cap):
                    self.set_config(MAX_SEARCH_RESULTS=cap)
                    expected = reply_as_before(USER, term, CHANNEL)
                    self.assertEqual(self.reply(term), expected)

    def test_off_ignores_the_folder_cap(self):
        expected = reply_as_before(USER, "some song", CHANNEL)
        self.assertEqual(self.reply("some song", SEARCH_FOLDER_MAX_CHARS=12), expected)

    def test_only_true_turns_it_on(self):
        """A stray string from a hand-edited file is not a yes."""
        expected = reply_as_before(USER, "some song", CHANNEL)
        self.assertEqual(self.reply("some song", SEARCH_SHOW_FOLDER="false"), expected)


class OffKeepsTheAudioFallback(SearchCase):
    """The path where the budget would cut a row and its audio tail goes first."""

    long_name = "Example Artist - " + "Very Long Title " * 19 + ".mp3"
    folders = [(heading("Rock", "Some Band", "1999 - Some Album"), [
        ("Short Song.mp3", "4.1MB 4m31s 320/44.1/JS"),
        (long_name, "9.9MB 7m2s ~245/44.1/JS"),
    ])]

    def test_off_is_byte_for_byte_the_old_reply_on_the_fallback_path(self):
        expected = reply_as_before(USER, "very long title", CHANNEL)
        got = self.reply("very long title")
        self.assertEqual(got, expected)
        # Control: this really is the fallback path.
        self.assertNotIn("7m2s", got[-1])
        self.assertIn(self.long_name + "  ::INFO:: 9.9MB", got[-1])

    def test_on_sends_the_very_same_row(self):
        expected_rows = self.rows(reply_as_before(USER, "very long title", CHANNEL))
        got = self.reply("very long title", SEARCH_SHOW_FOLDER=True)
        self.assertEqual(self.rows(got), expected_rows)
        border, separator, textbox, _r, _b, value, _a, _x = theme.blocks()
        self.assertEqual([self.text(line) for line in self.from_lines(got)],
                         [f"From: {border} {separator} {textbox} {value}" + list_mod.LIST_FOLDER_PREFIX
                          + "Rock" + BS + "Some Band" + BS + "1999 - Some Album"
                          + f" {separator} {border} "])


class OnGroupsByFolder(SearchCase):

    def on(self, term="some song", **settings):
        return self.reply(term, SEARCH_SHOW_FOLDER=True, **settings)

    def test_the_exact_reply(self):
        self.set_config(THEME="plain")
        old = {}
        for line in reply_as_before(USER, "some song", CHANNEL)[1:]:
            old[re.search(r"(\S+)  ::INFO::", line).group(1)] = line
        lines = self.on()
        prefix = "From:    D:" + BS + "MEDIA" + BS
        self.assertEqual(lines[1:], [
            old["00-some_song-loose.flac"],
            f"PRIVMSG {USER} :" + prefix + "Live" + BS + "Zeta Band" + BS + "2003 - Live Somewhere   \r\n",
            old["10-some_song-live.mp3"],
            f"PRIVMSG {USER} :" + prefix + "Rock" + BS + "Alpha Band" + BS + "1990 - First Album   \r\n",
            old["05-some_song.flac"],
            f"PRIVMSG {USER} :" + prefix + "Rock" + BS + "Zeta Band" + BS + "2001 - Last Album   \r\n",
            old["01-some_song.flac"],
            old["07-some_song-edit.flac"],
        ])
        self.assertIn("Search Result:", lines[0])

    def test_one_from_line_per_distinct_folder_before_its_first_file(self):
        lines = self.on(THEME="plain")
        froms = [self.text(line) for line in self.from_lines(lines)]
        self.assertEqual(len(froms), len(set(froms)), "a folder got a second From: line")
        self.assertEqual(len(froms), 3)
        # Every row after a From: line, up to the next one, is from that folder.
        by_name = {name: folder for folder, files in FOLDERS for name, _size in files}
        current = None
        for line in lines[1:]:
            text = self.text(line)
            if self.label(line).startswith("From: "):
                current = self.label(line)
                continue
            name = re.search(f"!{NICK} (.+?)  ::INFO::", text).group(1)
            folder = by_name[name]
            if folder is None:
                self.assertIsNone(current, "a row with no folder came after a From: line")
            else:
                self.assertIsNotNone(current)
                self.assertTrue(current.endswith(folder.rstrip(BS)), (current, folder))

    def test_the_groups_are_sorted_by_folder(self):
        froms = [self.label(line) for line in self.from_lines(self.on())]
        self.assertEqual(froms, sorted(froms, key=str.casefold))

    def test_a_folder_line_never_starts_with_a_bang(self):
        """Under every theme, and with each of the roles the From: line's
        own frame actually uses set to an attacker-shaped string (#1249
        review): the raw wire text - not just the colour-stripped label -
        starts with "From: ", under BOTH readings control codes could hide
        a leading one behind.

        BORDER/SEPARATOR/TEXTBOX are the roles this template puts BEFORE
        "From: " in theme.blocks()'s own order - VALUE and ACCENT land
        after it and were already safe; ALERT is unused by this template.
        The confirmed repro: CUSTOM_THEME_BORDER = "!othernick" put that
        text, unescaped, at the very start of the line - a real request to
        another bot, pasted into a channel."""
        roles = ("BORDER", "SEPARATOR", "TEXTBOX", "VALUE", "ACCENT")
        for name in theme.THEMES:
            for role in roles:
                overrides = {f"CUSTOM_THEME_{role}": "!othernick"}
                with self.subTest(theme=name, role=role):
                    for line in self.from_lines(self.on(THEME=name, **overrides)):
                        text = self.text(line)
                        # The raw wire text, lstripped but with control codes
                        # still in it - what a leading \x03 would hide a "!"
                        # behind, the gap an earlier version of this test had.
                        self.assertTrue(text.lstrip().startswith("From: "), text)
                        # And the same check again after stripping codes, in
                        # case a role's override ever precedes "From: " with
                        # something that is itself a control code sequence.
                        self.assertTrue(self.label(line).startswith("From: "), line)

    def test_the_result_lines_are_unchanged(self):
        expected = self.rows(reply_as_before(USER, "some song", CHANNEL))
        got = self.rows(self.on())
        self.assertEqual(sorted(got), sorted(expected))
        self.assertEqual(len(got), len(expected))

    def test_the_header_is_unchanged(self):
        expected = reply_as_before(USER, "some song", CHANNEL)[0]
        self.assertEqual(self.on()[0], expected)

    def test_max_search_results_counts_files_only(self):
        lines = self.on(MAX_SEARCH_RESULTS=2)
        self.assertEqual(len(self.rows(lines)), 2)
        self.assertIn("Sending: " + chr(3) + "04" + "2 ", lines[0])
        # The same two files the old reply sent, and one From: line for the
        # one folder among them that has a heading.
        self.assertEqual(sorted(self.rows(lines)),
                         sorted(self.rows(reply_as_before(USER, "some song", CHANNEL))))
        self.assertEqual(len(self.from_lines(lines)), 1)
        self.assertEqual(len(lines), 1 + 2 + 1)

    def test_a_result_with_no_folder_gets_no_from_line(self):
        lines = self.on("loose")
        self.assertEqual(len(lines), 2)
        self.assertEqual(self.from_lines(lines), [])

    def test_zero_matches_sends_nothing(self):
        self.assertEqual(self.on("nothing-like-this"), [])
        self.assertEqual(self.on("---"), [])

    def test_the_folder_is_the_lists_heading_never_a_disk_path(self):
        for line in self.from_lines(self.on(THEME="plain")):
            label = self.label(line)
            self.assertTrue(label.startswith("From:"), label)
            self.assertIn(list_mod.LIST_FOLDER_PREFIX, label.split("From:", 1)[1])
            self.assertNotIn(self.tree.music, line)

    def test_plain_sends_no_colour_code_on_a_folder_line(self):
        for line in self.from_lines(self.on(THEME="plain")):
            text = self.text(line)
            self.assertIsNone(re.search("[" + chr(2) + chr(3) + chr(15) + chr(22) + chr(31) + "]", text), text)

    def test_the_line_is_framed_like_the_header(self):
        """Reported: the From: line did not follow the theme. "From: " now
        comes before the frame rather than after it (#1249 review, a
        confirmed finding: a free-text BORDER/SEPARATOR/TEXTBOX override put
        unescaped text at the very start of the line) - the frame itself is
        otherwise unchanged, still closing the same way the header does."""
        palette = theme.THEMES["midnight"]
        for line in self.from_lines(self.on(THEME="midnight")):
            text = self.text(line)
            self.assertTrue(text.startswith(f"From: {palette['border']} {palette['separator']} "
                                            f"{palette['textbox']} {palette['value']}"), text)
            self.assertTrue(text.endswith(f" {palette['separator']} {palette['border']} "), text)


class TheLeftCut(SearchCase):

    def test_at_the_cap_the_folder_is_whole(self):
        folder = "D:" + BS + "MEDIA" + BS + "abcdefghij"
        self.assertEqual(len(folder), 19)
        self.assertEqual(list_mod.search_folder_text(folder + BS, 19), folder)

    def test_one_past_the_cap_it_is_cut_from_the_left(self):
        folder = "D:" + BS + "MEDIA" + BS + "abcdefghij"
        shown = list_mod.search_folder_text(folder + BS, 18)
        self.assertEqual(shown, "..." + folder[-15:])
        self.assertEqual(len(shown), 18)
        self.assertTrue(shown.endswith("abcdefghij"))

    def test_the_issues_example(self):
        folder = heading("Metal", "Somebandallica", "1988 - ...And Some Justice")
        shown = list_mod.search_folder_text(folder, 36)
        self.assertEqual(shown, "...allica" + BS + "1988 - ...And Some Justice")
        self.assertEqual(len(shown), 36)

    def test_a_tiny_or_broken_cap_still_cuts(self):
        """0 would slice nothing off (text[-0:] is all of it); a cap that
        shows only the dots tells nobody anything."""
        folder = heading("Rock", "Zeta Band", "2001 - Last Album")
        floor = list_mod.SEARCH_FOLDER_MIN_CHARS
        for cap in (0, -5, 3, floor - 1, "junk", None):
            with self.subTest(cap=cap):
                shown = list_mod.search_folder_text(folder, cap)
                if cap in ("junk", None):
                    self.assertEqual(shown, folder.rstrip(BS))
                else:
                    self.assertEqual(shown, "..." + folder.rstrip(BS)[-(floor - 3):])
                    self.assertEqual(len(shown), floor)

    def test_the_reply_carries_the_cut(self):
        lines = self.reply("some song", SEARCH_SHOW_FOLDER=True, THEME="plain",
                           SEARCH_FOLDER_MAX_CHARS=20)
        froms = [self.label(line) for line in self.from_lines(lines)]
        self.assertIn("From:    ...2001 - Last Album", froms)
        for text in froms:
            self.assertTrue(text.startswith("From:"), text)
            self.assertEqual(len(text.split("From:", 1)[1].lstrip()), 20, text)

    def test_the_line_still_fits_the_irc_budget(self):
        self.set_config(SEARCH_FOLDER_MAX_CHARS=5000)
        long_heading = heading("Rock", "Band " * 150, "Album")
        self.folders = [(long_heading, [("01-some_song.flac", "1.0MB")])]
        write_master_list(self.tree.lists, NICK, self.folders)
        lines = self.reply("some song", SEARCH_SHOW_FOLDER=True)
        froms = self.from_lines(lines)
        self.assertEqual(len(froms), 1)
        self.assertLessEqual(len(froms[0].encode("utf-8")), announce.IRC_LINE_BUDGET)
        self.assertTrue(self.label(froms[0]).startswith("From: "))


class TheSettingsPage(unittest.TestCase):

    def test_both_settings_sit_next_to_max_search_results(self):
        for _id, _title, names in webserver.SETTINGS_CATEGORIES:
            if "MAX_SEARCH_RESULTS" in names:
                at = names.index("MAX_SEARCH_RESULTS")
                self.assertEqual(names[at + 1:at + 3],
                                 ["SEARCH_SHOW_FOLDER", "SEARCH_FOLDER_MAX_CHARS"])
                return
        self.fail("MAX_SEARCH_RESULTS is in no category")

    def test_the_default_cap(self):
        self.assertEqual(config.SEARCH_FOLDER_MAX_CHARS, 80)


if __name__ == "__main__":
    unittest.main()
