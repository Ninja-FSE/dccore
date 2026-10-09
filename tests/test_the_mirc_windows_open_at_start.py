"""dccore.mrc opened no window when mIRC started (#1201).

`on *:START` only loaded the settings. @DCCore appeared 8 seconds after
mIRC connected, and only when the script still wanted the console open
(`wantopen`, cleared when the window is closed), auto-connect was on and
the connection was the bot's network; DCCore Chat appeared only for an
arriving line and the Downloads window only when asked for. An operator
who had closed @DCCore once started mIRC to no DCCore window at all.

Now START opens each window ticked under "Open when mIRC starts" in the
options dialog - @DCCore by default, Chat and Downloads not - minimised
(`-n`) with its button at the end of the switchbar (`-z`), through the
same openers as always, so the font, the background and the panel are the
usual ones. It only opens windows: it never sets `wantopen` and never
dials, so the connect logic is what it was. Until the console connects
@DCCore says what it is waiting for, in its title and in one line.

mIRC cannot run here, so like every other test of the script this reads
it: the statements, with comment lines left out, since the comments name
the very identifiers the tests look for.
"""

import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")


def script():
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def block(opening):
    """The text from `opening` to the closing brace at column 0."""
    text = script()
    start = text.index(opening + "\n")
    return text[start:].split("\n}\n", 1)[0]


def code(opening):
    """The statements of a block, stripped, without its comment lines."""
    lines = [line.strip() for line in block(opening).splitlines()[1:]]
    return [line for line in lines if line and not line.startswith(";")]


def alias(name):
    return code("alias %s {" % name)


def window_switches(statement):
    """The switches of a `window -xyz <name>` inside a statement."""
    found = re.search(r"\bwindow -([a-z0-9]+) ", statement)
    return found.group(1) if found else None


class TheDefaults(unittest.TestCase):

    def test_dccore_opens_at_start_and_the_other_two_do_not(self):
        init = alias("dccore.init")

        self.assertIn("dccore.default start.main 1", init)
        self.assertIn("dccore.default start.chat 0", init)
        self.assertIn("dccore.default start.downloads 0", init)

    def test_the_defaults_come_after_the_saved_settings_are_loaded(self):
        """dccore.default only fills a gap, so an upgrade keeps a choice made."""
        init = alias("dccore.init")
        loaded = init.index("if ($isfile($dccore.ini)) { hload dccore $dccore.ini }")

        for line in ("dccore.default start.main 1", "dccore.default start.chat 0",
                     "dccore.default start.downloads 0"):
            self.assertGreater(init.index(line), loaded, line)


class StartOpensEachWindowBehindItsSetting(unittest.TestCase):

    def test_start_loads_the_settings_then_opens_the_windows(self):
        self.assertEqual(code("on *:START: {"), ["dccore.init", "dccore.atstart"])

    def test_each_window_is_behind_its_own_setting(self):
        start = alias("dccore.atstart")

        self.assertEqual(start[0], "if ($dccore.opt(start.main)) && (!$window($dccore.win)) {")
        self.assertEqual(start[1], "dccore.window start")
        self.assertIn("if ($dccore.opt(start.chat)) { dccore.chat.window start }", start)
        self.assertIn("if ($dccore.opt(start.downloads)) { dccore.dl.window start }", start)

    def test_every_opener_is_called_with_start(self):
        start = "\n".join(alias("dccore.atstart"))

        for opener in ("dccore.window", "dccore.chat.window", "dccore.dl.window"):
            calls = re.findall(r"\b%s\b(.*?)(?: \}|$)" % re.escape(opener), start, re.M)
            self.assertEqual(calls, [" start"], opener)

    def test_dccore_opened_at_start_is_minimised_at_the_end_of_the_switchbar(self):
        window = alias("dccore.window")
        branch = window.index("if ($1 == start) {")

        self.assertEqual(window[branch + 1], "if ($dccore.opt(panel)) { window -enzl30 $dccore.win }")
        self.assertEqual(window[branch + 2], "else { window -enz $dccore.win }")
        self.assertEqual(window[branch + 3], "}")

    def test_dccore_opened_otherwise_is_the_window_it_always_was(self):
        window = alias("dccore.window")

        self.assertIn("elseif ($dccore.opt(panel)) { window -el30 $dccore.win }", window)
        self.assertIn("else { window -e $dccore.win }", window)

    def test_dccore_opened_at_start_gets_the_same_font_background_and_panel(self):
        """The start branch only picks the switches; the set-up after it runs
        for both, so nothing is duplicated and nothing is left out."""
        window = alias("dccore.window")
        last_window = max(i for i, line in enumerate(window) if window_switches(line))

        self.assertEqual(window[last_window + 1:], [
            "if ($dccore.opt(font)) { font $dccore.win $dccore.fontsize Lucida Console }",
            "dccore.background",
            "dccore.title",
            "dccore.panel",
        ])

    def test_chat_opened_at_start_is_minimised_at_the_end_of_the_switchbar(self):
        chat = alias("dccore.chat.window")

        self.assertIn("elseif ($1 == start) { window -enzl16 $dccore.chat.win }", chat)
        self.assertIn("if ($1 == quiet) { window -enl16 $dccore.chat.win }", chat)
        self.assertIn("else { window -el16 $dccore.chat.win }", chat)

    def test_downloads_opened_at_start_is_minimised_at_the_end_of_the_switchbar(self):
        downloads = alias("dccore.dl.window")

        self.assertIn("if ($1 == start) { window -nzl64 $dccore.dl.win }", downloads)
        self.assertIn("else { window -l64 $dccore.dl.win }", downloads)

    def test_a_downloads_window_already_open_is_not_brought_up_at_start(self):
        """START also runs when the script is reloaded; the menu's open
        still brings an open window to the front."""
        downloads = alias("dccore.dl.window")

        self.assertEqual(downloads[:4], [
            "if ($window($dccore.dl.win)) {",
            "if ($1 != start) { window -a $dccore.dl.win }",
            "return",
            "}",
        ])

    def test_every_window_opened_at_start_has_n_and_z_and_no_other_has_z(self):
        openers = {
            "dccore.window": alias("dccore.window"),
            "dccore.chat.window": alias("dccore.chat.window"),
            "dccore.dl.window": alias("dccore.dl.window"),
        }
        seen_start = 0
        for name, lines in openers.items():
            inside_start = False
            for line in lines:
                if line == "if ($1 == start) {":
                    inside_start = True
                    continue
                if inside_start and line == "}":
                    inside_start = False
                    continue
                switches = window_switches(line)
                if switches is None or switches == "a":
                    continue
                if inside_start or re.match(r"(?:else)?if \(\$1 == start\) \{ ", line):
                    seen_start += 1
                    self.assertIn("n", switches, (name, line))
                    self.assertIn("z", switches, (name, line))
                else:
                    self.assertNotIn("z", switches, (name, line))
        self.assertEqual(seen_start, 4)


class StartDoesNotConnect(unittest.TestCase):
    """Opening at start must leave the connect logic exactly as it was."""

    FORBIDDEN = ("wantopen", "dccore.connect", "dcc chat", "dccore connect",
                 "dccore pair", ".timerdccoreRetry", "dccore.send")

    def test_nothing_on_the_start_path_dials_or_wants_the_console_open(self):
        path = (code("on *:START: {") + alias("dccore.atstart") + alias("dccore.window")
                + alias("dccore.chat.window") + alias("dccore.dl.window"))
        for line in path:
            for word in self.FORBIDDEN:
                self.assertNotIn(word, line)

    def test_the_waiting_line_only_reads_wantopen(self):
        waiting = "\n".join(alias("dccore.waiting"))

        self.assertNotIn("dccore.set", waiting)
        self.assertNotIn("hadd", waiting)
        self.assertNotIn("dccore.connect", waiting)
        self.assertEqual(waiting.count("wantopen"), 1)
        self.assertIn("if ($dccore.opt(wantopen)) && ($dccore.opt(auto)) {", waiting)

    def test_connect_still_dials_only_when_the_console_is_wanted(self):
        connect = code("on *:CONNECT: {")

        self.assertEqual(connect[:4], [
            "if ($dccore.here) && ($dccore.opt(wantopen)) && ($dccore.opt(auto)) && ($dccore.bot != $null) {",
            "hadd dccore.live tries 0",
            ".timerdccoreRetry 1 8 dccore.connect",
            "}",
        ])

    def test_connect_then_tells_a_window_opened_at_start_what_it_waits_for(self):
        self.assertEqual(code("on *:CONNECT: {")[4:], ["if ($dccore.here) { dccore.title }"])

    def test_the_window_closed_handler_still_clears_wantopen(self):
        """Closing @DCCore still stops the reconnects; the setting, not the
        close, decides whether it opens next time."""
        self.assertIn("dccore.set wantopen 0", code("on *:CLOSE:@DCCore: {"))


class DCCoreSaysWhatItIsWaitingFor(unittest.TestCase):

    def test_start_writes_the_state_line_into_a_window_it_opened(self):
        start = alias("dccore.atstart")

        self.assertEqual(start[2], "if ($dccore.st(state) == $null) { dccore.sys $dccore.waiting }")
        self.assertEqual(start[3], "}")

    def test_the_title_says_it_before_the_first_dial(self):
        title = alias("dccore.title")
        line = "if ($dccore.st(state) == $null) { titlebar $dccore.win %bot $dccore.dot $dccore.waiting | return }"

        self.assertIn(line, title)
        self.assertLess(title.index(line), [i for i, l in enumerate(title)
                                            if l.startswith("if ($dccore.st(state) != in)")][0])

    def test_the_states_in_the_order_they_are_decided(self):
        waiting = alias("dccore.waiting")

        self.assertEqual(waiting, [
            "if ($dccore.bot == $null) { return no bot paired yet: /dccore pair <botnick> }",
            "var %cid = $dccore.cid",
            "var %net = $iif($dccore.opt(net),$dccore.opt(net),IRC)",
            "if (%cid == $null) { return Waiting for the bot: not connected to %net yet }",
            "if ($scid(%cid).server == $null) { return Waiting for the bot: not connected to %net yet }",
            "if ($dccore.opt(wantopen)) && ($dccore.opt(auto)) { return Waiting for the bot: it is not answering yet }",
            "return Waiting for the bot: the console is not open - /dccore connect opens it",
        ])


class TheOptionsDialogHasTheThreeCheckboxes(unittest.TestCase):

    SETTINGS = (("start.main", 701, "@DCCore"),
                ("start.chat", 702, "@DCCore-Chat"),
                ("start.downloads", 703, "@DCCore-Downloads"))

    def test_the_checkboxes_are_in_their_own_box(self):
        table = block("dialog dccore.opt {")

        self.assertIn('box "Open when mIRC starts (minimised)", 700, 5 274 312 24', table)
        for _setting, cid, label in self.SETTINGS:
            self.assertRegex(table, r'\n  check "%s", %d, \d+ 284 \d+ 10\n' % (re.escape(label), cid))

    def test_init_ticks_each_from_its_setting(self):
        init = code("on *:dialog:dccore.opt:init:0: {")

        for setting, cid, _label in self.SETTINGS:
            self.assertIn("if ($dccore.opt(%s)) { did -c dccore.opt %d }" % (setting, cid), init)

    def test_ok_saves_each_into_its_setting(self):
        ok = code("on *:dialog:dccore.opt:sclick:1: {")
        saved = ok.index("dccore.save")

        for setting, cid, _label in self.SETTINGS:
            line = "hadd dccore %s $did(dccore.opt,%d).state" % (setting, cid)
            self.assertIn(line, ok)
            self.assertLess(ok.index(line), saved, line)

    def test_the_buttons_moved_below_the_new_box(self):
        table = block("dialog dccore.opt {")

        self.assertIn("size -1 -1 322 316", table)
        self.assertIn('button "OK", 1, 232 302 40 12, ok default', table)


class TheScriptVersionMovedOn(unittest.TestCase):

    def test_the_version_is_at_least_1_13(self):
        """The changelog tells operators to reload the script for this; the
        bot reads the version from `hello` (adminchat compares tuples)."""
        found = re.search(r"^alias dccore\.ver \{ return (\d+)\.(\d+)(?:\.\d+)? \}$", script(), re.M)

        self.assertIsNotNone(found)
        self.assertGreaterEqual((int(found.group(1)), int(found.group(2))), (1, 13))


if __name__ == "__main__":
    unittest.main()
