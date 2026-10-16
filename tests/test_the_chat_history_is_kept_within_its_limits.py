"""DCCore Chat's history is kept within its limits (#1401).

The history (#1393) wrote one file per channel that only ever grew. Now a
line goes to a file per channel per month (`DCCore-Chat\\<channel>\\YYYY-MM.log`),
and two limits - the BOT's settings CHAT_HISTORY_DAYS and CHAT_HISTORY_MAX_MB,
sent to the script as `DCCORE CHATHIST <days> <max_mb>` - delete whole months:
every month that ended more than the days ago, then, while the history folder
is over the size, the oldest months, never the month running. The script keeps
the last limits in dccore.ini, applies them when they change and when the chat
window opens (once a day at most), and deletes nothing before a bot has sent
any.

mIRC cannot run here. The aliases are run by the interpreter of
tests/test_dccore_chat_keeps_a_history_and_counts_unread.py, against a real
temp folder, with the few things pruning needs added below: $findfile with a
command per file, $file().size, /remove, token lists. Every nick, channel and
path is invented.
"""

import fnmatch
import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.test_dccore_chat_keeps_a_history_and_counts_unread import (  # noqa: E402
    HEARD, NOW, PEER, TODAY_ID, Frame, Mirc, MircError, Session, asctime, code_body, script_text, statements)

DAY = 86400
MB = 1048576


class PruningMirc(Mirc):
    """The chat interpreter, plus what pruning uses. Anything else it is not
    taught still fails the test, as before."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.removed = []
        self.refuse_remove = None      # a path fragment whose /remove fails, as a locked file's does
        self.found_with = []

    def identifier(self, name, raw, prop, frame):
        lower = name.lower()
        if lower == "findfile":
            # $findfile(dir,wildcard,0,depth,command): the command run for
            # every file found, with $1- the file - evaluated per file, so it
            # is taken here unevaluated.
            assert raw is not None and len(raw) == 5, raw
            folder, wildcard, number, depth = (self.eval_text(part, frame) for part in raw[:4])
            assert number == "0", "only the command form is modelled"
            self.found_with.append((folder, wildcard, depth))
            for path in self.files_under(folder, wildcard, int(depth)):
                self.command(raw[4].strip(), Frame(path.split(" ")))
            return "0"
        if lower == "file" and prop == "size":
            path = self.eval_text(raw[0], frame)
            return str(os.path.getsize(self.host(path))) if os.path.isfile(self.host(path)) else "0"
        return super().identifier(name, raw, prop, frame)

    def files_under(self, folder, wildcard, depth):
        """What $findfile finds: files matching `wildcard` in `folder` and,
        down to `depth` levels in all, in its folders - in a fixed order, so a
        test never depends on the file system's."""
        root = self.host(folder)
        found = []
        for here, dirs, files in os.walk(root):
            level = 1 if here == root else os.path.relpath(here, root).count(os.sep) + 2
            if level > depth:
                dirs[:] = []
                continue
            found += [os.path.join(here, f) for f in files if fnmatch.fnmatch(f, wildcard)]
        return sorted(path.replace(os.sep, "\\") for path in found)

    # -- identifiers
    def id_remove(self, text, *parts):
        for part in parts:
            text = text.replace(part, "")
        return text

    def id_numtok(self, text, char):
        return str(len([t for t in text.split(chr(int(char))) if t]))

    def id_addtok(self, text, token, char):
        tokens = [t for t in text.split(chr(int(char))) if t]
        if token.lower() not in [t.lower() for t in tokens]:
            tokens.append(token)
        return chr(int(char)).join(tokens)

    def id_sorttok(self, text, char, how):
        assert how == "n", "only the numeric sort is modelled"
        tokens = [t for t in text.split(chr(int(char))) if t]
        return chr(int(char)).join(sorted(tokens, key=float))

    def id_gettok(self, text, number, char):
        ranged = re.fullmatch(r"(\d+)-", number)
        if ranged:
            tokens = [t for t in text.split(chr(int(char))) if t]
            return chr(int(char)).join(tokens[int(ranged.group(1)) - 1:])
        return super().id_gettok(text, number, char)

    # -- commands
    def cmd_noop(self, value):
        pass

    def cmd_hmake(self, value):
        self.tables[value.split(" ")[0].lower()] = {}

    def cmd_hfree(self, value):
        self.tables.pop(value.split(" ")[0].lower(), None)

    def cmd_remove(self, value):
        path, _ = self.path_and_rest(value)
        if self.refuse_remove and self.refuse_remove in path:
            raise MircError("/remove: unable to delete " + path)
        os.remove(self.host(path))
        self.removed.append(path)


class LimitsSession(Session):
    """A chat session on the pruning interpreter, its tables kept."""

    def __init__(self, test, text=None):
        super().__init__(test, text)
        tables = self.mirc.tables
        self.mirc = PruningMirc(self.mirc.text, self.logs)
        self.mirc.tables = tables

    def month_file(self, channel, month, size=10):
        """A month's file of `channel` ("2026-10"), `size` bytes."""
        folder = os.path.join(self.folder, channel)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, month + ".log")
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        return path

    def other_file(self, name, size=10):
        path = os.path.join(self.folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        return path

    def months_left(self, channel):
        folder = os.path.join(self.folder, channel)
        return sorted(f[:-4] for f in os.listdir(folder)) if os.path.isdir(folder) else []

    def limits(self, days, mb):
        self.mirc.tables["dccore"].update({"chat.hist.days": str(days), "chat.hist.mb": str(mb)})

    def prune(self):
        self.mirc.call("dccore.chat.hist.prune", [])


def this_month(offset=0):
    """yyyy-mm of the fake $ctime's month, `offset` months on (negative: back)."""
    year, month = int(asctime(NOW, "yyyy")), int(asctime(NOW, "mm")) + offset
    while month < 1:
        year, month = year - 1, month + 12
    while month > 12:
        year, month = year + 1, month - 12
    return "%04d-%02d" % (year, month)


class ALineGoesToItsMonthsFile(unittest.TestCase):

    def setUp(self):
        self.s = LimitsSession(self)

    def test_the_file_is_named_for_the_channel_and_the_lines_month(self):
        name = self.s.mirc.call("dccore.chat.hist.file", ["#lobby-one", str(TODAY_ID)])
        self.assertEqual(name, self.s.mirc.logdir + "DCCore-Chat\\#lobby-one\\%s.log" % asctime(TODAY_ID // 1000, "yyyy-mm"))

    def test_a_line_from_last_month_goes_to_last_months_file(self):
        line_id = (NOW - 40 * DAY) * 1000
        name = self.s.mirc.call("dccore.chat.hist.file", ["#lobby-one", str(line_id)])
        self.assertTrue(name.endswith("\\#lobby-one\\%s.log" % asctime(line_id // 1000, "yyyy-mm")), name)
        self.assertNotEqual(asctime(line_id // 1000, "yyyy-mm"), asctime(NOW, "yyyy-mm"))

    def test_your_line_to_every_channel_and_a_private_one_have_their_folders(self):
        every = self.s.mirc.call("dccore.chat.hist.file", ["-", str(TODAY_ID)])
        private = self.s.mirc.call("dccore.chat.hist.file", ["@" + PEER, str(TODAY_ID)])
        self.assertIn("\\every-channel\\", every)
        self.assertIn("\\@%s\\" % PEER, private)

    def test_the_channels_folder_is_made_when_a_line_is_written(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "into its own folder")
        self.assertEqual(self.s.months_left(HEARD), [asctime(TODAY_ID // 1000, "yyyy-mm")])


class TheBotSendsTheLimits(unittest.TestCase):

    def setUp(self):
        self.s = LimitsSession(self)
        self.dccore = self.s.mirc.tables["dccore"]

    def send(self, *fields):
        self.s.mirc.call("dccore.chat.hist.limits", [str(f) for f in fields])

    def test_they_are_kept_in_dccore_ini_and_applied_at_once(self):
        self.send(100, 50)
        self.assertEqual((self.dccore["chat.hist.days"], self.dccore["chat.hist.mb"]), ("100", "50"))
        self.assertIn(("stub", "dccore.save", ""), self.s.mirc.trace)
        self.assertEqual(len(self.s.calls("dccore.chat.hist.prune")), 1)

    def test_the_same_limits_again_change_nothing(self):
        self.send(100, 100)
        self.send(100, 100)
        self.assertEqual(len(self.s.calls("dccore.chat.hist.prune")), 1)

    def test_a_change_of_either_applies_them_again(self):
        self.send(100, 100)
        self.send(100, 20)
        self.send(30, 20)
        self.assertEqual(len(self.s.calls("dccore.chat.hist.prune")), 3)

    def test_anything_but_two_whole_numbers_of_0_or_more_is_ignored(self):
        for fields in (("x", 1), (1,), (-1, 5), (5, -1), ()):
            self.send(*fields)
        self.assertNotIn("chat.hist.days", self.dccore)
        self.assertEqual(self.s.calls("dccore.chat.hist.prune"), [])

    def test_a_fraction_is_taken_whole(self):
        self.send("100.7", "50.2")
        self.assertEqual((self.dccore["chat.hist.days"], self.dccore["chat.hist.mb"]), ("100", "50"))

    def test_the_bots_line_reaches_them(self):
        """DCCORE CHATHIST through the script's own dispatcher."""
        self.s.mirc.call("dccore.structured", ["CHATHIST", "90", "40"])
        self.assertEqual((self.dccore["chat.hist.days"], self.dccore["chat.hist.mb"]), ("90", "40"))


class HowLong(unittest.TestCase):
    """CHAT_HISTORY_DAYS: a month that ended more than the days ago goes."""

    def setUp(self):
        self.s = LimitsSession(self)
        for back in range(0, 7):
            self.s.month_file(HEARD, this_month(-back))
            self.s.month_file("@" + PEER, this_month(-back))

    def test_every_channels_months_that_ended_before_the_limit_go(self):
        self.s.limits(100, 0)
        self.s.prune()
        # the fake clock is mid-January: 100 days back is in early October,
        # so October and later stay and September and before go
        cut = asctime(NOW - 100 * DAY, "yyyy-mm")
        kept = [this_month(-back) for back in range(6, -1, -1) if this_month(-back) >= cut]
        self.assertEqual(self.s.months_left(HEARD), kept)
        self.assertEqual(self.s.months_left("@" + PEER), kept)
        self.assertIn(cut, kept)

    def test_a_longer_limit_keeps_more(self):
        self.s.limits(200, 0)
        self.s.prune()
        self.assertEqual(len(self.s.months_left(HEARD)), 7)

    def test_0_days_is_no_limit(self):
        self.s.limits(0, 0)
        self.s.prune()
        self.assertEqual(len(self.s.months_left(HEARD)), 7)
        self.assertEqual(self.s.mirc.removed, [])


class HowMuch(unittest.TestCase):
    """CHAT_HISTORY_MAX_MB: the oldest months go while the folder is over."""

    def setUp(self):
        self.s = LimitsSession(self)

    def test_the_oldest_months_go_until_the_folder_is_under(self):
        for back, size in ((3, 500_000), (2, 500_000), (1, 500_000), (0, 100_000)):
            self.s.month_file(HEARD, this_month(-back), size)
        self.s.limits(0, 1)                 # 1 MB: 1,600,000 bytes, then 1,100,000, are over
        self.s.prune()
        self.assertEqual(self.s.months_left(HEARD), [this_month(-1), this_month(0)])

    def test_a_month_goes_in_every_channel_at_once(self):
        self.s.month_file(HEARD, this_month(-2), 700_000)
        self.s.month_file("@" + PEER, this_month(-2), 10)
        self.s.month_file(HEARD, this_month(-1), 500_000)
        self.s.limits(0, 1)
        self.s.prune()
        self.assertEqual(self.s.months_left(HEARD), [this_month(-1)])
        self.assertEqual(self.s.months_left("@" + PEER), [])

    def test_the_month_running_is_never_deleted_for_its_size(self):
        self.s.month_file(HEARD, this_month(-1), 10)
        self.s.month_file(HEARD, this_month(0), 2 * MB)
        self.s.limits(0, 1)
        self.s.prune()
        self.assertEqual(self.s.months_left(HEARD), [this_month(0)])

    def test_under_the_limit_nothing_goes(self):
        for back in range(5):
            self.s.month_file(HEARD, this_month(-back), 100_000)
        self.s.limits(0, 1)
        self.s.prune()
        self.assertEqual(len(self.s.months_left(HEARD)), 5)

    def test_the_days_go_first_and_count_against_the_size(self):
        self.s.month_file(HEARD, this_month(-6), 900_000)     # past 100 days
        self.s.month_file(HEARD, this_month(-1), 500_000)
        self.s.month_file(HEARD, this_month(0), 10)
        self.s.limits(100, 1)
        self.s.prune()
        # with the old month gone for its age the folder is under 1 MB
        self.assertEqual(self.s.months_left(HEARD), [this_month(-1), this_month(0)])


class OnlyMonthFilesAreTouched(unittest.TestCase):

    def setUp(self):
        self.s = LimitsSession(self)
        self.old = this_month(-6)

    def test_recent_txt_the_old_flat_files_and_anything_else_stay(self):
        keep = [self.s.other_file("recent.txt"), self.s.other_file("#lobby-one.log"),
                self.s.other_file(os.path.join(HEARD, "notes.txt")),
                self.s.other_file(os.path.join(HEARD, "2026-1x.log")),
                self.s.other_file(os.path.join(HEARD, "deep", self.old + ".log"))]
        gone = self.s.month_file(HEARD, self.old)
        self.s.limits(1, 0)
        self.s.prune()
        self.assertFalse(os.path.exists(gone))
        for path in keep:
            self.assertTrue(os.path.exists(path), path)

    def test_it_looks_in_the_history_folder_and_one_level_down(self):
        self.s.month_file(HEARD, self.old)
        self.s.limits(1, 0)
        self.s.prune()
        folder, wildcard, depth = self.s.mirc.found_with[0]
        self.assertEqual((folder, wildcard, depth), (self.s.mirc.logdir + "DCCore-Chat", "????-??.log", "2"))


class NothingGoesWithoutLimits(unittest.TestCase):

    def setUp(self):
        self.s = LimitsSession(self)
        self.path = self.s.month_file(HEARD, this_month(-12), 5 * MB)

    def test_before_a_bot_has_sent_any(self):
        self.s.prune()
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.s.mirc.found_with, [], "the folder was not even read")

    def test_with_both_at_0(self):
        self.s.limits(0, 0)
        self.s.prune()
        self.assertTrue(os.path.exists(self.path))

    def test_with_no_history_folder(self):
        self.s.limits(1, 1)
        os.remove(self.path)
        os.rmdir(os.path.dirname(self.path))
        os.rmdir(self.s.folder)
        self.s.prune()
        self.assertEqual(self.s.mirc.found_with, [])


class WhenItRuns(unittest.TestCase):

    def setUp(self):
        self.s = LimitsSession(self)
        self.s.limits(100, 100)

    def test_once_a_day_when_the_window_opens(self):
        self.s.open_window()
        self.s.mirc.command("window -c @DCCore-Chat", Frame([]))     # closed, and opened again
        self.s.open_window()
        self.assertEqual(len(self.s.calls("dccore.chat.hist.prune")), 1)
        self.assertEqual(self.s.live("chat.pruned"), asctime(NOW, "yyyymmdd"))

    def test_the_day_is_marked_even_when_there_is_nothing_to_do(self):
        """No folder yet: the next open the same day does not ask again."""
        self.s.mirc.tables["dccore"].pop("chat.hist.days")
        self.s.open_window()
        self.assertEqual(self.s.live("chat.pruned"), asctime(NOW, "yyyymmdd"))

    def test_again_on_another_day(self):
        self.s.mirc.tables["dccore.live"]["chat.pruned"] = asctime(NOW - DAY, "yyyymmdd")
        self.s.open_window()
        self.assertEqual(len(self.s.calls("dccore.chat.hist.prune")), 1)

    def test_also_for_a_history_turned_off(self):
        """Turning it off stops the writing; the files written still keep
        to the limits."""
        old = self.s.month_file(HEARD, this_month(-6))
        self.s.mirc.tables["dccore"]["chat.history"] = "0"
        self.s.open_window()
        self.assertFalse(os.path.exists(old))

    def test_the_window_opener_runs_it_after_the_replay(self):
        opener = statements(code_body(script_text(), "alias dccore.chat.window"))
        self.assertLess(opener.index("dccore.chat.hist.replay"), opener.index("dccore.chat.hist.daily"))


class AFileThatCannotBeDeleted(unittest.TestCase):

    def test_stops_the_pass_quietly_and_the_next_one_tries_again(self):
        s = LimitsSession(self)
        locked = s.month_file(HEARD, this_month(-6))
        s.limits(1, 0)
        s.mirc.refuse_remove = this_month(-6)
        s.prune()                                   # no error out of it
        self.assertTrue(os.path.exists(locked))
        self.assertNotIn("dccore.chathist", s.mirc.tables, "its table was freed")
        s.mirc.refuse_remove = None
        s.prune()
        self.assertFalse(os.path.exists(locked))


class TheDocsSayIt(unittest.TestCase):

    def test_admin_console_names_the_settings_their_defaults_and_where(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            doc = handle.read()
        part = doc[doc.index("- **How long and how much (#1401):**"):doc.index("- **Unread:**")]
        for phrase in ("`CHAT_HISTORY_DAYS`", "`CHAT_HISTORY_MAX_MB`", "**100** days", "**100** MB",
                       "**Settings → Private messages**", "**mIRC** side", "`DCCORE CHATHIST`",
                       "never the month running", "nothing is\n  deleted"):
            self.assertIn(phrase, part)


if __name__ == "__main__":
    unittest.main()
