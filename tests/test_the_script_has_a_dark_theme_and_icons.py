"""The script's dark theme and icons, and their setting on the mIRC side (#1368).

dccore.mrc 1.20: /dccore theme auto|dark|light and /dccore icons on|off, and
the same two on the control panel's mIRC side - the Window page's Theme
frame - saved by Apply like the rest of this mIRC's own settings. The dark
dialogs come through dll\\dccore_ui.dll and the icons from
dll\\dccore_icons.dll; tests/test_the_theme_dlls_are_the_build_the_script_
pins.py holds the files to what they say they are. This file reads what the
script does with them: where the setting sits and how it is filled and
saved, that a change is drawn at once and only a change, that the marks in
the menus name icons the file has and are nothing without it, that every
page of the layout has its icon, and that the dark dialogs stand aside
where mIRC draws dark itself or the dll is not the one.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "scripts", "mirc")):
    if path not in sys.path:
        sys.path.insert(0, path)

import build_settings_window as generator  # noqa: E402
import settings_window_layout as layout  # noqa: E402
from tests.support import without_menu_icons  # noqa: E402
from tests.test_the_mirc_menu_has_every_command import alias_body, block  # noqa: E402

SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")
DOC = os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md")


def raw():
    """The script as it is, marks and all."""
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def doc():
    with io.open(DOC, encoding="utf-8") as handle:
        return " ".join(handle.read().split())


def statements(body):
    return [line.strip() for line in body.split("\n")[1:-1] if line.strip() and not line.strip().startswith(";")]


def icon_names():
    return re.search(r"(?m)^alias dccore\.ico\.names \{ return ([a-z ]+) \}$", raw()).group(1).split()


class TheSettingIsOnTheMircSide(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.block, cls.builder = generator.generate()

    def test_the_theme_frame_is_on_the_window_page_of_the_mirc_side(self):
        page = next(number for number, names in self.builder.page_widgets.items() if "theme" in names)
        _number, group, name = next(entry for entry in self.builder.pages if entry[0] == page)
        self.assertEqual(name, "Window")
        self.assertEqual(group, max(g for _, g, _ in self.builder.pages), "the mIRC side is the last group")
        self.assertEqual(layout.GROUPS[-1][0], "mIRC")
        self.assertIn("  hadd dccore.swm p.%d.w winlook theme" % page, self.block)

    def test_its_controls_are_a_menu_and_a_tick(self):
        self.assertEqual(generator.WIDGETS["theme"], (
            (1690, "text", "Theme", 0, 2, 120, 8, ""),
            (1691, "combo", "", 134, 0, 96, 80, "drop"),
            (1692, "check", "Icons in the menus and the control panel's page list", 0, 15, 358, 10, ""),
        ))
        # this mIRC's own: editable whatever the bot says, as the window's
        # other locals are
        self.assertIn("theme", generator.LOCAL_WIDGETS)
        table = block("dialog dccore.set {")
        self.assertIn(" 1691, ", table)
        self.assertIn('check "Icons in the menus and the control panel\'s page list", 1692, ', table)

    def test_the_layout_says_what_it_is(self):
        window = next(sections for name, sections in layout.GROUPS[-1][1] if name == "Window")
        theme = next(items for title, items in window if title == "Theme")
        self.assertIn("Follow mIRC takes mIRC's own dark mode (7.79 and later)", theme[0]["note"])
        self.assertIn("/dccore theme says what applies and why", theme[0]["note"])
        self.assertEqual(theme[1], {"widget": "theme"})


class TheSettingIsFilledAndSaved(unittest.TestCase):

    def test_the_menu_is_filled_with_the_three_and_the_tick_from_the_setting(self):
        lines = statements(alias_body("dccore.sw.opt.fill"))
        at = lines.index("did -r dccore.set 1691")
        self.assertEqual(lines[at + 1:at + 5], [
            "did -a dccore.set 1691 Follow mIRC",
            "did -a dccore.set 1691 Dark",
            "did -a dccore.set 1691 Light",
            "did -c dccore.set 1691 $findtok(auto dark light,$dccore.theme.mode,1,32)",
        ])
        self.assertIn("if ($dccore.theme.icons) { did -c dccore.set 1692 }", lines)

    def test_the_mode_is_one_of_three_whatever_the_file_says(self):
        self.assertIn("return $iif($istok(dark light,%m,32),%m,auto)", alias_body("dccore.theme.mode"))

    def test_a_change_of_either_counts_as_a_change(self):
        self.assertIn("$did(dccore.set,1691).sel $did(dccore.set,1692).state", alias_body("dccore.sw.opt.sig"))

    def test_apply_saves_them_and_draws_the_change_once(self):
        save = statements(alias_body("dccore.sw.opt.save"))
        self.assertIn("var %theme = $gettok(auto dark light,$did(dccore.set,1691).sel,32), "
                      "%icons = $iif($did(dccore.set,1692).state == 1,1,0), %retheme = 0", save)
        theme = "if (%theme != $null) && (%theme != $dccore.theme.mode) { hadd dccore theme %theme | %retheme = 1 }"
        icons = "if (%icons != $iif($dccore.theme.icons,1,0)) { hadd dccore icons %icons | %retheme = 1 }"
        redraw = "if (%retheme) { dccore.theme.refresh }"
        for line in (theme, icons, redraw):
            self.assertIn(line, save)
        # saved before dccore.save writes dccore.ini; drawn again after the
        # window's own redraw, and only when one of them changed
        self.assertLess(save.index(icons), save.index("dccore.save"))
        self.assertGreater(save.index(redraw), save.index("dccore.save"))
        self.assertEqual(sum(1 for line in save if "dccore.theme.refresh" in line), 1)

    def test_the_defaults_are_auto_with_the_icons_on(self):
        text = raw()
        self.assertIn("  dccore.default theme auto\n", text)
        self.assertIn("  dccore.default icons 1\n", text)

    def test_icons_unset_read_as_on(self):
        # /reload -rs runs neither on LOAD nor on START, so a script
        # reloaded over an older one has no "icons" at all: on, as the
        # default is, and off only when set to 0
        self.assertIn("\nalias dccore.theme.icons { return $iif($dccore.opt(icons) == 0,$false,$true) }\n", raw())
        # and nothing reads the raw option but that
        text = raw()
        self.assertEqual(text.count("$dccore.opt(icons)"), 1, "read $dccore.theme.icons, not the raw option")


class TheCommandsAndTheMenu(unittest.TestCase):

    def test_the_commands_change_or_say_how_things_stand(self):
        body = alias_body("dccore")
        self.assertIn("if ($istok(auto dark light,$2,32)) { dccore.set theme $2 | dccore.theme.refresh }", body)
        self.assertIn("elseif ($2 != $null) { dccore.sys Use: /dccore theme auto, dark or light. | return }", body)
        self.assertIn("if ($istok(on off,$2,32)) { dccore.set icons $iif($2 == on,1,0) | dccore.theme.refresh }", body)
        self.assertEqual(body.count("dccore.theme.status"), 2)

    def test_they_are_documented(self):
        with io.open(DOC, encoding="utf-8") as handle:
            text = handle.read()
        self.assertRegex(text, r"(?m)^/dccore theme \[auto\|dark\|light\] ")
        self.assertRegex(text, r"(?m)^/dccore icons \[on\|off\] ")
        self.assertIn("### Dark theme and icons", text)
        self.assertIn("the **Theme** menu on the control panel's mIRC side (**Window** page)", doc())
        self.assertIn("the theme (dark, light, or following mIRC's own dark mode - see *Dark theme and icons* below) "
                      "with the icons in the menus and the control panel's page list", doc())

    def test_the_window_menu_offers_the_theme_with_the_current_one_ticked(self):
        menu = block("menu @DCCore {")
        lines = [line.strip() for line in menu.split("\n")]
        self.assertIn("..$iif($dccore.theme.mode == auto,$style(1)) Follow mIRC:dccore theme auto", lines)
        self.assertIn("..$iif($dccore.theme.mode == dark,$style(1)) Dark:dccore theme dark", lines)
        self.assertIn("..$iif($dccore.theme.mode == light,$style(1)) Light:dccore theme light", lines)
        self.assertIn("..$iif($dccore.theme.icons,$style(1)) Icons in the menus and the control panel:"
                      "dccore icons $iif($dccore.theme.icons,off,on)", lines)

    def test_a_change_is_drawn_on_what_is_open(self):
        refresh = alias_body("dccore.theme.refresh")
        self.assertIn("dccore.theme.menus", refresh)
        self.assertIn("var %w = dccore.dq dccore.setl dccore.dcxcheck, %i = 1", refresh)
        self.assertIn("if ($dialog($gettok(%w,%i,32))) { dccore.theme.dialog $gettok(%w,%i,32) }", refresh)
        # the control panel is drawn once, when it opens, and says so
        self.assertIn("if ($dialog(dccore.set)) { dccore.sys The control panel takes the change the next time it opens. }",
                      refresh)
        self.assertIn("if ($window($dccore.win)) { dccore.panel }", refresh)
        self.assertIn("if ($window($dccore.dl.win)) { dccore.dl.draw }", refresh)


class TheMenuMarks(unittest.TestCase):

    def test_every_mark_names_an_icon_the_file_has(self):
        marks = set(re.findall(r"\$dccore\.mi\(([a-z]+)\)", raw()))
        self.assertGreater(len(marks), 20)
        self.assertEqual(marks - set(icon_names()), set(), "a menu item names an icon that is not in the file")

    def test_a_mark_is_nothing_without_the_icons(self):
        lines = statements(alias_body("dccore.mi"))
        self.assertEqual(lines[:2], ["if (!$dccore.st(mi.tried)) { dccore.theme.menus }",
                                     "if (!$dccore.st(mi.on)) { return }"])
        self.assertIn("if (%n) { return %n $+ $chr(11) }", lines)

    def test_the_first_menu_after_a_reload_starts_the_hook_once(self):
        # /reload -rs runs no on START: the first mark asked for starts the
        # hook, and the attempt is remembered whatever it gave
        menus = statements(alias_body("dccore.theme.menus"))
        self.assertEqual(menus[0], "hadd dccore.live mi.tried 1")

    def test_the_marks_are_all_the_theme_adds_to_a_menu(self):
        # so the tests that read the menus read them without the marks
        for menu in re.findall(r"\nmenu [^\n]+ \{\n(.*?)\n\}", raw(), re.S):
            self.assertNotIn("$dccore.mi(", without_menu_icons(menu))

    def test_the_hook_goes_on_with_the_pinned_dll_and_off_without_loading_one(self):
        on = statements(alias_body("dccore.theme.menus"))
        self.assertIn("if ($dccore.theme.icons) && ($isfile($dccore.theme.iconfile)) { %on = 1 }", on)
        self.assertIn("if (%on) && ($dccore.theme.ui) {", on)
        self.assertIn("hadd dccore.live mi.on $iif($dccore.theme.call(MenuIcons,$dccore.theme.iconfile) == OK,1,0)",
                      on)
        off = alias_body("dccore.theme.menusoff")
        self.assertIn("if ($dccore.st(mi.on)) && ($dccore.theme.uiloaded) { noop $dccore.theme.call(MenuIcons,off) }",
                      off)
        loaded = statements(alias_body("dccore.theme.uiloaded"))
        self.assertIn("while (%i <= $dll(0)) {", loaded)
        self.assertIn("if ($nopath($dll(%i)) == dccore_ui.dll) { return $true }", loaded)
        # the first version drew the menus with DCX's XPopup, every menu in
        # mIRC with them; nothing of it is left to hand back
        text = raw()
        self.assertNotIn("mi.loaded", text)
        self.assertNotIn("mpopup", text)

    def test_the_hook_starts_with_the_script_and_stops_with_it(self):
        text = raw()
        self.assertIn("alias dccore.theme.start { dccore.theme.menus }", text)
        self.assertIn("alias dccore.theme.stop { dccore.theme.menusoff }", text)
        # each event's own statements, so a call elsewhere cannot stand in
        self.assertIn("dccore.theme.start", statements(block("on *:START: {")))
        self.assertIn("dccore.theme.start", statements(block("on *:LOAD: {")))
        unload = statements(block("on *:UNLOAD: {"))
        self.assertIn("dccore.theme.stop", unload)
        # off before the tables it reads are freed
        self.assertLess(unload.index("dccore.theme.stop"),
                        unload.index("if ($hget(dccore.live)) { hfree dccore.live }"))


class ThePageIcons(unittest.TestCase):

    def test_every_page_of_the_layout_has_an_icon(self):
        pagemap = re.search(r"(?m)^alias dccore\.theme\.pagemap \{ return (.*) \}$", raw()).group(1)
        pairs = dict(pair.split("=") for pair in pagemap.split(","))
        names = icon_names()
        for _group, pages in layout.GROUPS:
            for page, _sections in pages:
                self.assertIn(page, pairs, "the page %r has no icon in dccore.theme.pagemap" % page)
                self.assertIn(pairs[page], names, "the page %r names an icon the file does not have" % page)

    def test_a_page_has_none_until_the_file_is_in_the_list(self):
        lines = statements(alias_body("dccore.theme.pageicon"))
        self.assertEqual(lines[0], "if (!$dccore.sw.s(theme.icons)) { return 0 }")
        pageicons = alias_body("dccore.theme.pageicons")
        self.assertIn("-w +n -1 $dccore.theme.iconfile", pageicons)
        self.assertIn("hadd dccore.sws theme.icons 1", pageicons)

    def test_the_list_gets_them_in_a_timer_of_its_own_after_the_window_is_made(self):
        # a DCX command that fails halts what called it: never the window
        self.assertIn("if ($dccore.theme.icons) && ($isfile($dccore.theme.iconfile)) "
                      "{ .timerdccoreThemeIcons -m 1 0 dccore.theme.pageicons }", alias_body("dccore.theme.dcxset"))
        self.assertIn("on *:dialog:dccore.set:init:0: { dccore.sw.init | dccore.theme.dialog dccore.set init | "
                      "dccore.theme.dcxset }", raw())


class TheDarkDialogs(unittest.TestCase):

    def test_every_dialog_is_themed_when_it_is_made(self):
        text = raw()
        for dialog in ("dccore.dq", "dccore.dcxcheck", "dccore.setl", "dccore.set"):
            self.assertRegex(text, r"(?m)^on \*:dialog:%s:init:0: \{ .*dccore\.theme\.dialog %s init"
                             % (re.escape(dialog), re.escape(dialog)), dialog)

    def test_it_stands_aside_where_mirc_draws_dark_or_the_dll_is_not_the_one(self):
        lines = statements(alias_body("dccore.theme.dialog"))
        self.assertEqual(lines[1:5], [
            "if (!$dialog(%d)) { return }",
            "if ($dccore.theme.mircdark) { return }",
            "if ($2 == init) && (!$dccore.theme.dark) { return }",
            # light: only through a dll already loaded, never by loading one
            "if (!$dccore.theme.dark) && (!$dccore.theme.uiloaded) { return }",
        ])
        self.assertEqual(lines[-1], "noop $dccore.theme.call(ApplyTheme,$dialog(%d).hwnd $iif($dccore.theme.dark,1,0) %x)")
        self.assertEqual(statements(alias_body("dccore.theme.mircdark")), [
            "if ($version < 7.79) { return $false }",
            # a condition, right whether $darkmode answers $true or 1
            "return $iif($darkmode,$true,$false)",
            ":error",
            "reseterror",
            "return $false",
        ])

    def test_the_dll_is_checked_before_it_is_loaded(self):
        # in this order, every step reached: missing, too old, 64-bit, the
        # hash taken again until mIRC holds the dll, then compared
        self.assertEqual(statements(alias_body("dccore.theme.uiwhy")), [
            "if (!$isfile($dccore.theme.uifile)) { return dll\\dccore_ui.dll is missing }",
            "if ($version < 7.45) { return it needs mIRC 7.45 or later }",
            "if ($bits == 64) { return this mIRC is 64-bit and dll\\dccore_ui.dll is 32-bit }",
            "if ($dccore.st(ui.sha) == $null) || (!$dccore.theme.uiloaded) "
            "{ hadd dccore.live ui.sha $sha256($dccore.theme.uifile,2) }",
            "if ($dccore.st(ui.sha) != $dccore.theme.uisha256) "
            "{ return dll\\dccore_ui.dll is not the build this script was shipped with }",
        ])
        self.assertIn("alias dccore.theme.ui { return $iif($dccore.theme.uiwhy == $null,$true,$false) }", raw())

    def test_every_call_into_the_dll_goes_through_the_pin(self):
        # one place calls dccore_ui.dll, and it asks first: no caller can
        # reach the dll before the check, whatever order it puts things in
        self.assertEqual(statements(alias_body("dccore.theme.call")), [
            "if (!$dccore.theme.ui) { return }",
            "return $dll($dccore.theme.uifile,$1,$2-)",
            # a dll that cannot be opened would stop the script
            ":error",
            "reseterror",
            "return",
        ])
        code = "\n".join(line for line in raw().split("\n") if not line.lstrip().startswith(";"))
        # every $dll that calls a function - a file, a function, its data -
        # names DCX or goes through here; $dll(N) and $dll(0) only ask what
        # mIRC has loaded
        called = re.findall(r"\$dll\(([^,()]*(?:\([^()]*\))?[^,()]*),", code)
        self.assertEqual(sorted(set(called)), ["$dccore.dcx.path", "$dccore.theme.uifile"])
        self.assertEqual(called.count("$dccore.theme.uifile"), 1)
        # and the /dll command form is DCX's alone
        commands = re.findall(r"(?m)(?:^|[|{])\s*dll\s+(\S+)", code)
        self.assertEqual(set(commands), {"$qt($dccore.dcx.path)"})

    def test_without_the_dll_the_dark_theme_leaves_the_control_panel_light(self):
        # the DCX parts follow the dialogs: dark only where they are drawn
        # dark, by mIRC's own dark mode or by dccore_ui.dll
        self.assertEqual(statements(alias_body("dccore.theme.dialogsdark")), [
            "if (!$dccore.theme.dark) { return $false }",
            "if ($dccore.theme.mircdark) { return $true }",
            "return $dccore.theme.ui",
        ])
        self.assertEqual(statements(alias_body("dccore.theme.sys"))[0],
                         "if ($dccore.theme.dialogsdark) { return $dccore.theme.rgb($1) }")
        self.assertIn("if (!$dccore.theme.dialogsdark) { return }", statements(alias_body("dccore.theme.dcxset")))
        # what is made after the window - a page's frames, the tables (#1382)
        # - takes its dark colours from the same answer, asked once as the
        # window is made
        self.assertIn("hadd dccore.sws dark $iif($dccore.theme.dialogsdark,1,0)", statements(alias_body("dccore.sw.dcx")))
        self.assertIn("if ($dccore.sw.s(dark)) { dccore.theme.dcxlist %id }", statements(alias_body("dccore.sw.lv.make")))
        self.assertIn("if ($dccore.sw.s(dark)) { dccore.theme.dcxtext %title }",
                      statements(alias_body("dccore.sw.dcxframes")))
        # nothing else of the control panel's DCX parts asks the theme alone
        for name in ("dccore.theme.sys", "dccore.theme.dcxset", "dccore.theme.dcxlists", "dccore.sw.enable",
                     "dccore.sw.dcx", "dccore.sw.lv.make", "dccore.sw.dcxframes"):
            self.assertNotIn("$dccore.theme.dark)", alias_body(name), name)

    def test_a_colour_is_left_alone_unless_it_would_vanish(self):
        lines = statements(alias_body("dccore.theme.legible"))
        self.assertEqual(lines[:3], [
            "if ($1 !isnum 0-15) { return $1 }",
            "if ($dccore.theme.mode == light) { return $1 }",
            "if (!$istok(1 2 5 6,$int($dccore.theme.bgidx($2)),32)) { return $1 }",
        ])
        # black, navy, green, maroon, purple, blue on a dark background: the
        # light neighbour of each
        self.assertIn("var %at = $findtok(1 2 3 5 6 12,$int($1),1,32)", lines)
        self.assertIn("return $iif(%at,$gettok(0 11 9 4 13 11,%at,32),$1)", lines)


if __name__ == "__main__":
    unittest.main()
