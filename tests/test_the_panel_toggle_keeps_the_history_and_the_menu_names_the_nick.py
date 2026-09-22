"""#582 and #584: two bugs in dccore.mrc's window, found by the 2026-09-20 audit.

CI has no mIRC, so this reads the script (as test_the_bots_window_in_mirc.py
does) and pins the shape of each fix.

#582 - toggling the side panel (Options, /dccore panel, right-click) rebuilt
the window by closing it and THEN reading its lines, which do not exist any
more: the history was gone, the first empty /echo halted the alias, and the
"rebuilding" flag - which makes the CLOSE handler ignore the close - stayed set
for good, so @DCCore could not be closed. The lines are now copied out first,
an empty one is kept as a non-breaking space, and the flag is a start time that
stops counting after a few seconds.

#584 - the right-click "Queue of" / "Clear the queue of" read the nick back
from the panel text, where it is cut or padded to nine characters: a long nick
was truncated (the bot cleared the queue of a nick that does not exist), a
short one came back with its padding. The number that starts a queue row names
the row in the status the panel was drawn from, and a sending row's nine
characters are matched against the slots; the whole nick comes from there.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")


def script():
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def alias_body(name):
    text = script()
    start = text.index("alias " + name + " {")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError(name)


class TheRebuild(unittest.TestCase):

    def test_the_lines_are_copied_out_before_the_window_is_closed(self):
        body = alias_body("dccore.rebuild")
        self.assertLess(body.index("hadd dccore.rb %i %t"), body.index("window -c $dccore.win"))

    def test_the_lines_are_written_back_from_the_copy_not_from_the_new_window(self):
        body = alias_body("dccore.rebuild")
        after_close = body[body.index("window -c $dccore.win"):]
        self.assertIn("$hget(dccore.rb,%i)", after_close)
        self.assertNotIn("$line($dccore.win,%i)", after_close, "reading the empty new window is the bug")

    def test_an_empty_line_is_kept_as_a_non_breaking_space(self):
        """/hadd and /echo both refuse an empty text; either would halt the alias."""
        body = alias_body("dccore.rebuild")
        self.assertIn("if (%t == $null) { var %t = $dccore.nbsp }", body)
        self.assertLess(body.index("if (%t == $null)"), body.index("hadd dccore.rb"))

    def test_the_table_is_made_fresh_and_freed(self):
        body = alias_body("dccore.rebuild")
        self.assertIn("if ($hget(dccore.rb)) { hfree dccore.rb }", body)
        self.assertLess(body.index("hmake dccore.rb"), body.index("hadd dccore.rb"))
        self.assertLess(body.index("echo -i2"), body.rindex("hfree dccore.rb"))

    def test_the_flag_is_a_start_time_and_is_cleared_at_the_end(self):
        body = alias_body("dccore.rebuild")
        self.assertIn("hadd dccore.live rebuilding $ticks", body)
        self.assertTrue(body.rstrip().endswith("hadd dccore.live rebuilding 0\n}"))

    def test_close_ignores_only_a_rebuild_that_is_recent(self):
        text = script()
        handler = text[text.index("on *:CLOSE:@DCCore: {"):]
        handler = handler[:handler.index("\n}") + 2]
        self.assertIn("if ($dccore.st(rebuilding)) && ($calc($ticks - $dccore.st(rebuilding)) < 5000) { return }", handler)

    def test_nothing_else_still_sets_the_flag_to_one(self):
        self.assertNotIn("rebuilding 1\n", script())


class TheRightClickNick(unittest.TestCase):
    """The menu's nick is read with plain string functions, not a regex (a real
    mIRC matched the old pattern and returned an empty group), and the logic is
    checked here by running the same steps on rows built the way the panel
    builds them."""

    NBSP = "\xa0"

    # -- mIRC's own functions, for the few the script uses -------------------
    def gettok(self, text, n, sep=" "):
        parts = [p for p in text.split(sep)]
        return parts[n - 1] if 0 < n <= len(parts) else ""

    def mid(self, text, start, length):
        return text[start - 1:start - 1 + length]

    def fit(self, nick, width):
        return (nick + self.NBSP * width)[:width]

    def rfit(self, text, width):
        return (self.NBSP * width + text)[-width:]

    # -- the two aliases, step for step ----------------------------------------
    def selq(self, row, status):
        number = self.gettok(row, 1).replace(self.NBSP, "")
        if number.isdigit() and status.get("queue." + number) is not None:
            return self.gettok(status["queue." + number], 1)
        return ""

    def sels(self, row, status):
        if not row or ord(row[0]) != 62:
            return ""
        wanted = self.mid(row, 3, 9).replace(self.NBSP, "")
        if not wanted:
            return ""
        index = 1
        while status.get("slot.%d" % index) is not None:
            nick = self.gettok(status["slot.%d" % index], 1)
            if nick[:9] == wanted:
                return nick
            index += 1
        return ""

    def status(self):
        return {"queue.1": "longnickname1 4 0", "queue.2": "helen 1 0", "queue.12": "Ann 2 0",
                "slot.1": "Fearsie 4293984256 24618096018 4857150 Blue Beetle (2023).mkv",
                "slot.2": "longnickname1 100 200 300 Some File.flac"}

    def queue_row(self, index, nick, files):
        return self.rfit(str(index), 2) + " " + self.fit(nick, 9) + " " + files

    def test_a_queue_row_gives_the_whole_long_nick(self):
        self.assertEqual(self.selq(self.queue_row(1, "longnickname1", "4 files"), self.status()), "longnickname1")

    def test_a_short_nick_comes_back_without_its_padding(self):
        got = self.selq(self.queue_row(2, "helen", "1 file"), self.status())
        self.assertEqual(got, "helen")
        self.assertNotIn(self.NBSP, got)

    def test_a_two_digit_row_number_works(self):
        self.assertEqual(self.selq(self.queue_row(12, "Ann", "2 files"), self.status()), "Ann")

    def test_a_sending_row_gives_the_nick(self):
        row = "> " + self.fit("Fearsie", 9) + " 22.9GB  15%  4.68MB/s"
        self.assertEqual(self.sels(row, self.status()), "Fearsie")

    def test_the_row_from_the_users_own_diagnostic_works(self):
        """34 characters: '>', a space, 'F' ... - what a real mIRC reported."""
        row = "> Fearsie" + self.NBSP * 2 + " " + " 22.9GB  15%  4.68MB/s"
        self.assertEqual(self.mid(row, 3, 1), "F")
        self.assertEqual(self.sels(row, self.status()), "Fearsie")

    def test_a_long_nick_in_a_sending_row_is_matched_by_its_first_nine_characters(self):
        row = "> " + self.fit("longnickname1", 9) + " 1.2G   50%  1MB/s"
        self.assertEqual(self.sels(row, self.status()), "longnickname1")

    def test_rows_that_are_not_people_give_nothing(self):
        status = self.status()
        for row in ("Queue 1 (6 files)", "Sending 1/3", self.NBSP * 2 + "(2 free)", self.NBSP,
                    self.NBSP * 2 + "sent       8 / 60.2GB", self.NBSP * 2 + "... 4 more", "Today",
                    "Since 13:09", self.NBSP * 2 + "failed    0", ""):
            self.assertEqual(self.selq(row, status), "", row)
            self.assertEqual(self.sels(row, status), "", row)

    def test_a_queue_row_that_the_status_no_longer_has_gives_nothing(self):
        self.assertEqual(self.selq(self.queue_row(7, "gone", "1 file"), self.status()), "")

    def test_a_sending_row_for_a_slot_that_ended_gives_nothing(self):
        row = "> " + self.fit("Someoneelse", 9) + " 1G 5% 1MB/s"
        self.assertEqual(self.sels(row, self.status()), "")

    # -- what the script says ------------------------------------------------
    def test_neither_alias_uses_a_regex_any_more(self):
        for name in ("dccore.selq", "dccore.sels"):
            self.assertNotIn("$regex", alias_body(name), name)
            self.assertNotIn("{9}", alias_body(name), name)

    def test_selq_reads_the_number_and_looks_it_up(self):
        body = alias_body("dccore.selq")
        self.assertIn("var %t = $sline($dccore.win,1)", body)
        self.assertIn("$remove($gettok(%t,1,32),$chr(160))", body)
        self.assertIn("(%n isnum) && ($dccore.st(queue. $+ %n) != $null)", body)
        self.assertIn("return $gettok($dccore.st(queue. $+ %n),1,32)", body)

    def test_sels_takes_nine_characters_after_the_arrow_and_matches_the_slots(self):
        body = alias_body("dccore.sels")
        self.assertIn("$asc($left(%t,1)) != 62", body)
        self.assertIn("$remove($mid(%t,3,9),$chr(160))", body)
        self.assertIn("$dccore.st(slot. $+ %i)", body)
        self.assertIn("$left(%n,9) == %f", body)

    def test_the_lookups_use_the_same_keys_the_panel_is_drawn_from(self):
        text = script()
        panel = text[text.index("alias dccore.panel"):text.index("alias dccore.selq")]
        self.assertIn("$dccore.st(queue. $+ %i)", panel)
        self.assertIn("$dccore.st(slot. $+ %i)", panel)

    def test_a_queue_row_starts_with_its_number_as_the_lookup_expects(self):
        panel = script()
        panel = panel[panel.index("alias dccore.panel"):panel.index("alias dccore.selq")]
        self.assertGreaterEqual(panel.count("$dccore.rfit(%i,2) $dccore.fit($gettok(%l,1,32),9)"), 2,
                                "both the frozen and the ordinary queue row")
        self.assertIn("aline -l $dccore.opt(col.sends) $dccore.win > $dccore.fit($gettok(%l,1,32),9)", panel)

    def test_the_menu_still_offers_both(self):
        text = script()
        self.assertIn("Queue of $dccore.selq", text)
        self.assertIn("Clear the queue of $dccore.selq", text)
        self.assertIn("Queue of $dccore.sels", text)


if __name__ == "__main__":
    unittest.main()
