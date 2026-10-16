"""The theme's two DLLs are the build the script pins (#1368).

dccore.mrc darkens its dialogs and puts icons beside its menu items through
two DLLs of DCCore's own, scripts/mirc/dll/dccore_ui.dll and
dccore_icons.dll. As with dcx.dll (tests/test_the_bundled_dcx_dll_is_the_
pinned_release.py), a shipped binary is pinned in three places that are
checked against each other: the file itself, the README beside it, and the
script, which refuses a dccore_ui.dll whose SHA-256 differs. Change one
without the others and this fails.

It also holds the files to what the README promises: 32-bit, importing
nothing but Windows' own libraries, the icons' names in the script in the
order the README gives them and as many of them as the file holds, and the
sources beside the binaries.

The source can be ahead of the shipped file between a change to src/ and the
next build (as the 1.6 source was, #1378, until it was built with mingw). So
the README's list of calls is held to the source's exports, not the binary's; the README's version may trail the source's but
never lead it; and the day the README names the source's version, the shipped
DLL has to export exactly what the source does.
"""

import ast
import hashlib
import io
import os
import re
import struct
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.support import parse_source  # noqa: E402
from tests.test_the_bundled_dcx_dll_is_the_pinned_release import PE, WINDOWS_DLLS  # noqa: E402

DLL_DIR = os.path.join(REPO_ROOT, "scripts", "mirc", "dll")
UI = os.path.join(DLL_DIR, "dccore_ui.dll")
ICONS = os.path.join(DLL_DIR, "dccore_icons.dll")
README = os.path.join(DLL_DIR, "README.md")
UI_SOURCE = os.path.join(DLL_DIR, "src", "dccore_ui.c")
ICON_SOURCE = os.path.join(DLL_DIR, "src", "make_icons.py")
SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")

# Two beyond the DCX test's list, both on every Windows: the dark title bar
# is DWM's (dwmapi.dll), and a mingw build's C runtime is msvcrt.dll.
WINDOWS_DLLS_TOO = WINDOWS_DLLS | {"dwmapi.dll", "msvcrt.dll"}
RT_GROUP_ICON = 14


def read(path, mode="r"):
    if "b" in mode:
        with open(path, "rb") as handle:
            return handle.read()
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def sha256(path):
    return hashlib.sha256(read(path, "rb")).hexdigest()


def resource_ids(pe, resource_type):
    """The ids under one type in the dll's resource tree: a directory is
    sixteen bytes of header with the counts of named and numbered entries,
    then eight bytes per entry; an entry's offset is from the tree's root,
    and its high bit says it is a directory itself."""
    root = pe.offset(pe.directory(2))

    def entries(at):
        named, numbered = struct.unpack_from("<HH", pe.data, at + 12)
        for index in range(named + numbered):
            yield struct.unpack_from("<II", pe.data, at + 16 + 8 * index)

    for name, offset in entries(root):
        if name == resource_type and offset & 0x80000000:
            return [entry_name for entry_name, _ in entries(root + (offset & 0x7FFFFFFF))]
    return []


def icon_names_in(text, pattern):
    found = re.search(pattern, text)
    assert found, pattern
    return found.group(1).split()


def strip_c_comments(source):
    """The C source without its comments, string and char literals kept, so a
    name in a comment is not taken for code."""
    out, at, end = [], 0, len(source)
    while at < end:
        char = source[at]
        if char in "\"'":
            stop = at + 1
            while stop < end and source[stop] != char:
                stop += 2 if source[stop] == "\\" else 1
            out.append(source[at:stop + 1])
            at = stop + 1
        elif source.startswith("/*", at):
            stop = source.find("*/", at + 2)
            stop = end if stop < 0 else stop + 2
            out.append(" " + "\n" * source.count("\n", at, stop))
            at = stop
        elif source.startswith("//", at):
            stop = source.find("\n", at)
            at = end if stop < 0 else stop
        else:
            out.append(char)
            at += 1
    return "".join(out)


def source_exports(code):
    """What dccore_ui.c exports: each MIRC_PROC(Name) the macro expands, and
    each function declared __declspec(dllexport) outside a #define."""
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
    procs = re.findall(r"(?m)^\s*MIRC_PROC\((\w+)\)", code)
    direct = re.findall(r"__declspec\(dllexport\)[\w\s*]*?\b(\w+)\s*\(", code)
    return procs, set(procs) | set(direct)


def readme_ui_section(text):
    parts = re.split(r"(?m)^## ", text)
    found = [part for part in parts if part.startswith("`dccore_ui.dll`")]
    assert len(found) == 1, "the README has no single section for dccore_ui.dll"
    return found[0]


def readme_calls(text):
    """The calls the README lists for dccore_ui.dll: the top-level bullets
    after "Calls, through mIRC's `$dll`:", up to the first paragraph that is
    neither a bullet nor indented under one."""
    section = readme_ui_section(text)
    intro = "Calls, through mIRC's `$dll`:"
    assert intro in section, "the README does not introduce the list of calls"
    calls = []
    for line in section.split(intro, 1)[1].splitlines():
        if not line.strip() or line.startswith((" ", "\t")):
            continue
        if line.startswith("- "):
            named = re.match(r"- `(\w+)[ `]", line)
            assert named, "a call without its name first: " + line
            calls.append(named.group(1))
        elif calls:
            break
    return calls


def readme_ui_build_line(text):
    """The words of the README's build line for dccore_ui.dll (one command,
    continued with backslashes)."""
    blocks = re.findall(r"```\n(.*?)```", readme_ui_section(text), re.S)
    lines = [block for block in blocks if "-o dccore_ui.dll" in block]
    assert len(lines) == 1, "the README has no single build line for dccore_ui.dll"
    return lines[0].replace("\\\n", " ").split()


def version_of(text):
    return tuple(int(part) for part in text.split()[1].split("."))


def readme_version(text):
    named = re.findall(r"`Version` - `(dccore_ui \d+\.\d+)`", text)
    assert len(named) == 1, "the README must name what Version answers exactly once, found %r" % named
    return named[0]


def source_version(code):
    named = re.findall(r'(?m)^#define DCCORE_UI_VERSION "(dccore_ui \d+\.\d+)"$', code)
    assert len(named) == 1, "dccore_ui.c must define DCCORE_UI_VERSION once"
    return named[0]


def export_names(pe):
    """The names in a PE file's export directory (data directory 0): its
    NumberOfNames at +24 and AddressOfNames, an array of name RVAs, at +32."""
    rva = pe.directory(0)
    if not rva:
        return set()
    at = pe.offset(rva)
    count, names = struct.unpack_from("<I", pe.data, at + 24)[0], struct.unpack_from("<I", pe.data, at + 32)[0]
    table = pe.offset(names)
    return {pe.text(struct.unpack_from("<I", pe.data, table + 4 * index)[0]) for index in range(count)}


def make_icons_tree():
    # shared with other tests through the cache: read and walk it, never change it
    return parse_source(read(ICON_SOURCE), ICON_SOURCE)


def make_icons_up_to_the_font(argv):
    """Runs make_icons.py's own statements from the top to the one that sets
    BOLD (the font) with sys.argv = argv, and returns its namespace. PIL is
    the only import left out: the font is chosen before anything is drawn,
    and this host need not have Pillow."""
    tree = make_icons_tree()
    body = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "PIL":
            continue
        body.append(node)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "BOLD" for t in node.targets):
            break
    else:
        raise AssertionError("make_icons.py never sets BOLD")
    namespace = {"__name__": "make_icons", "__file__": ICON_SOURCE}
    module = ast.Module(body=body, type_ignores=[])
    with mock.patch.object(sys, "argv", ["make_icons.py"] + list(argv)):
        exec(compile(module, ICON_SOURCE, "exec"), namespace)
    return namespace


class TheFilesAreTheOnesTheReadmeNames(unittest.TestCase):

    def test_the_readme_names_the_files_hashes(self):
        named = re.findall(r"`([0-9a-f]{64})`", read(README))
        self.assertEqual(named, [sha256(UI), sha256(ICONS)],
                         "scripts/mirc/dll/README.md must name the SHA-256 of dccore_ui.dll, then of dccore_icons.dll")

    def test_the_script_refuses_any_other_ui_dll(self):
        pinned = re.search(r"^alias dccore\.theme\.uisha256 \{ return ([0-9a-f]{64}) \}$", read(SCRIPT), re.M)
        self.assertIsNotNone(pinned, "dccore.mrc has no dccore.theme.uisha256")
        self.assertEqual(pinned.group(1), sha256(UI), "dccore.theme.uisha256 is not the shipped file's SHA-256")
        body = read(SCRIPT)
        self.assertIn("if ($dccore.st(ui.sha) != $dccore.theme.uisha256) { return dll\\dccore_ui.dll is not the build "
                      "this script was shipped with }", body)

    def test_the_readme_names_the_version_the_dll_answers(self):
        version = re.search(r"`Version` - `(dccore_ui \d+\.\d+)`", read(README))
        self.assertIsNotNone(version, "the README does not say what Version answers")
        self.assertIn(version.group(1).encode("ascii"), read(UI, "rb"))

    def test_the_folder_holds_the_two_and_nothing_else_binary(self):
        self.assertEqual({n.lower() for n in os.listdir(DLL_DIR) if n.lower().endswith(".dll")},
                         {"dccore_ui.dll", "dccore_icons.dll"})

    def test_the_build_is_the_same_file_every_time(self):
        # --no-insert-timestamp: the hash above can be checked against a
        # build of one's own, which is the point of shipping the source
        text = read(README)
        self.assertEqual(text.count("-Wl,--no-insert-timestamp"), 2, "both build lines need --no-insert-timestamp")
        self.assertIn("i686-w64-mingw32-gcc", text)


class TheFilesAreWhatTheReadmePromises(unittest.TestCase):

    def setUp(self):
        self.ui = PE(read(UI, "rb"))
        self.icons = PE(read(ICONS, "rb"))

    def test_both_are_32_bit(self):
        # A 64-bit mIRC cannot load them, as it cannot load dcx.dll
        for pe in (self.ui, self.icons):
            self.assertEqual(pe.machine, 0x14C)
            self.assertEqual(pe.magic, 0x10B)

    def test_they_need_nothing_but_windows(self):
        for name, pe in (("dccore_ui.dll", self.ui), ("dccore_icons.dll", self.icons)):
            wanted = {n.lower() for n in pe.imports() + pe.delay_imports()}
            self.assertTrue(wanted, "found no imports in " + name)
            self.assertEqual(wanted - WINDOWS_DLLS_TOO, set(),
                             name + " imports a dll that is not Windows' own; ship it too or build without it")

    def test_the_icon_file_holds_pictures_only(self):
        # Nothing of it is ever run: no exports beyond DllMain's file, a
        # resource tree with the icons in it
        self.assertEqual({n.lower() for n in self.icons.imports()}, {"kernel32.dll", "msvcrt.dll"})
        self.assertNotEqual(self.icons.directory(2), 0, "dccore_icons.dll has no resources")
        self.assertNotIn(b"ApplyTheme", read(ICONS, "rb"))

    def test_the_icons_are_the_ones_the_script_names_in_that_order(self):
        in_script = icon_names_in(read(SCRIPT), r"(?m)^alias dccore\.ico\.names \{ return ([a-z ]+) \}$")
        in_readme = icon_names_in(read(README), r"must list them in the same order: `([a-z ]+)`")
        self.assertEqual(in_script, in_readme)
        self.assertEqual(len(in_script), len(set(in_script)), "an icon is named twice")
        groups = resource_ids(self.icons, RT_GROUP_ICON)
        self.assertEqual(len(groups), len(in_script),
                         "dccore_icons.dll holds %d icons, the script names %d" % (len(groups), len(in_script)))
        self.assertEqual(sorted(groups), list(range(1, len(in_script) + 1)), "the icons are not numbered 1 up")
        self.assertIn("%d icons" % len(in_script), read(README))

    def test_the_drawing_script_draws_them_in_the_same_order(self):
        names = icon_names_in(read(SCRIPT), r"(?m)^alias dccore\.ico\.names \{ return ([a-z ]+) \}$")
        drawn = re.findall(r'\("([a-z]+)", [a-z_]+\)', read(ICON_SOURCE))
        self.assertEqual(drawn, names, "src/make_icons.py draws the icons in another order than the script names them")


class TheSourceIsBesideTheBinary(unittest.TestCase):

    def test_the_ui_source_is_what_the_script_relies_on(self):
        source = read(UI_SOURCE)
        # mIRC's calling shape, exported: $dll(file,ApplyTheme,...) and the rest
        self.assertIn("#define MIRC_PROC(name) __declspec(dllexport) int __stdcall name(HWND mWnd, HWND aWnd, "
                      "char *data, char *parms, BOOL show, BOOL nopause)", source)
        for export in ("ApplyTheme", "MenuIcons", "Version"):
            self.assertIn("MIRC_PROC(%s)" % export, source)
        for export in ("LoadDll", "UnloadDll"):
            self.assertRegex(source, r"__declspec\(dllexport\)[^\n]*\b%s\(" % export, export)
        # mKeep: mIRC keeps the dll loaded instead of dropping it after ten
        # idle minutes, which is what lets the script ask whether it is
        # loaded before turning the menu hook off (dccore.theme.uiloaded)
        self.assertIn("li->mKeep = TRUE", source)
        # the hook and the subclasses come off again when the dll goes
        self.assertIn("UnloadDll", source)

    def test_the_sources_and_the_readme_are_plain_ascii(self):
        for path in (UI_SOURCE, ICON_SOURCE, README, os.path.join(DLL_DIR, "src", "dll.c")):
            read(path, "rb").decode("ascii")

    def test_the_readme_says_what_a_missing_file_costs(self):
        text = read(README)
        self.assertIn("a missing `dccore_ui.dll` leaves the dialogs light", text)
        self.assertIn("a missing `dccore_icons.dll` leaves the menus", text)


class TheReadmeIsTheSourcesNextBuild(unittest.TestCase):
    """#1378: the source is the next build's; the README's calls, build line
    and version follow the source, and the shipped DLL catches up with it."""

    def setUp(self):
        self.readme = read(README)
        self.code = strip_c_comments(read(UI_SOURCE))

    def test_the_readme_lists_exactly_the_sources_calls(self):
        procs, _ = source_exports(self.code)
        self.assertEqual(len(procs), len(set(procs)), "a MIRC_PROC is defined twice")
        calls = readme_calls(self.readme)
        self.assertEqual(sorted(calls), sorted(procs),
                         "README's calls for dccore_ui.dll %s, the source's MIRC_PROC exports %s" % (calls, procs))

    def test_theme_menus_is_gone(self):
        # uxtheme's unnamed ordinals 135/136, for the whole process, never
        # called by the script: neither exported nor listed nor kept
        procs, exports = source_exports(self.code)
        self.assertNotIn("ThemeMenus", exports)
        self.assertNotIn("ThemeMenus", readme_calls(self.readme))
        self.assertIsNone(re.search(r"\b(ThemeMenus|theme_menus)\b", self.code), "dccore_ui.c still has ThemeMenus")

    def test_the_source_exports_mircs_entry_points_and_its_calls_only(self):
        _, exports = source_exports(self.code)
        self.assertEqual(exports, {"ApplyTheme", "LoadDll", "MenuIcons", "UnloadDll", "Version"})

    def test_the_header_comment_lists_the_same_calls(self):
        header = read(UI_SOURCE).split("*/", 1)[0]
        listed = header.split("Calls (mIRC's $dll / /dll):", 1)
        self.assertEqual(len(listed), 2, "dccore_ui.c's header has no list of calls")
        names = []
        for line in listed[1].splitlines()[1:]:
            if line.strip() in ("*", ""):
                break
            named = re.match(r" \*   (\w+)\b", line)
            if named:
                names.append(named.group(1))
        self.assertEqual(sorted(names), sorted(readme_calls(self.readme)))

    def test_the_script_calls_only_what_the_readme_lists(self):
        called = set(re.findall(r"\$dccore\.theme\.call\((\w+)", read(SCRIPT)))
        self.assertTrue(called, "dccore.mrc calls nothing through dccore.theme.call")
        self.assertEqual(called - set(readme_calls(self.readme)), set())

    def test_the_build_line_is_in_the_readme_only(self):
        # one place for one fact (CONVENTIONS rule 2): the header points to it
        for name in ("dccore_ui.c", "dll.c"):
            self.assertNotIn("i686-w64-mingw32-gcc", read(os.path.join(DLL_DIR, "src", name)),
                             "src/%s repeats the build line; it belongs in README.md" % name)
        self.assertIn("../README.md", read(UI_SOURCE).split("*/", 1)[0])

    def test_dwmapi_is_looked_up_not_linked(self):
        words = readme_ui_build_line(self.readme)
        self.assertEqual(words[0], "i686-w64-mingw32-gcc")
        self.assertNotIn("-ldwmapi", words)
        libraries = {word[2:] + ".dll" for word in words if word.startswith("-l")}
        self.assertTrue(libraries, "the build line links nothing")
        self.assertEqual(libraries - WINDOWS_DLLS, set(), "the build line links a library beyond Windows' own")
        self.assertNotRegex(self.code, r"#include\s*<dwmapi\.h>")
        self.assertNotRegex(self.code, r"(?<![\w\"])DwmSetWindowAttribute\s*\(",
                            "DwmSetWindowAttribute is called directly, which imports dwmapi.dll")
        self.assertRegex(self.code, r'LoadLibraryW\(L"dwmapi\.dll"\)')

    def test_the_readme_version_does_not_lead_the_source(self):
        self.assertRegex(self.code, r"MIRC_PROC\(Version\)\s*\{[^}]*\bDCCORE_UI_VERSION\b",
                         "Version does not answer DCCORE_UI_VERSION")
        readme, source = readme_version(self.readme), source_version(self.code)
        self.assertLessEqual(version_of(readme), version_of(source),
                             "the README names %s, the source is %s" % (readme, source))

    def test_the_shipped_dll_has_every_call_and_is_the_source_once_built(self):
        shipped = export_names(PE(read(UI, "rb")))
        self.assertEqual(set(readme_calls(self.readme)) - shipped, set(),
                         "the README lists a call the shipped dccore_ui.dll does not export")
        if version_of(readme_version(self.readme)) == version_of(source_version(self.code)):
            # the README names the source's version, so the shipped DLL is
            # that build (the README's version is in the DLL: see above)
            _, exports = source_exports(self.code)
            self.assertEqual(shipped, exports, "dccore_ui.dll is not the build of src/dccore_ui.c")


class TheIconScriptRunsAnywhere(unittest.TestCase):
    """#1378: make_icons.py finds a bold font on Linux, Windows and macOS,
    takes --font, and writes the preview the tree holds."""

    def strings(self):
        return [node.value for node in ast.walk(make_icons_tree())
                if isinstance(node, ast.Constant) and isinstance(node.value, str)]

    def test_no_font_path_of_one_linux_is_written_in(self):
        for value in self.strings():
            self.assertNotIn("/share/fonts", value.replace("\\", "/"), "a hard-coded font folder: %r" % value)

    def test_the_preview_is_the_one_in_the_tree(self):
        saved = []
        for node in ast.walk(make_icons_tree()):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "save"
                    and node.args and isinstance(node.args[0], ast.Call) and node.args[0].args):
                last = node.args[0].args[-1]
                if isinstance(last, ast.Constant) and isinstance(last.value, str):
                    saved.append(last.value)
        self.assertIn("icons-preview.png", saved)
        self.assertNotIn("preview.png", saved)
        self.assertTrue(os.path.isfile(os.path.join(DLL_DIR, "icons-preview.png")))
        self.assertIn("icons-preview.png", read(README))
        self.assertIsNone(re.search(r"(?<![\w-])preview\.png", read(README)), "the README still names preview.png")

    def test_it_takes_a_font(self):
        options = []
        for node in ast.walk(make_icons_tree()):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                options += [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
        self.assertIn("--font", options)

    def fake_home(self, top):
        # the user's own font folders are under the home: point it here
        # and the XDG data dirs at a folder with nothing in it
        return mock.patch.dict(os.environ, {"HOME": top, "USERPROFILE": top,
                                            "XDG_DATA_DIRS": os.path.join(top, "none"),
                                            "WINDIR": os.path.join(top, "Windows")})

    def test_font_names_the_font(self):
        with tempfile.TemporaryDirectory() as top:
            font = os.path.join(top, "Some Bold.ttf")
            open(font, "wb").close()
            with self.fake_home(top):
                namespace = make_icons_up_to_the_font([os.path.join(top, "build"), "--font", font])
            self.assertEqual(namespace["BOLD"], font)
            self.assertEqual(namespace["OUT"], os.path.join(top, "build"))

    def test_a_missing_font_is_named(self):
        with tempfile.TemporaryDirectory() as top:
            missing = os.path.join(top, "nothing.ttf")
            with self.fake_home(top), self.assertRaises(SystemExit) as stopped:
                make_icons_up_to_the_font(["--font", missing])
            self.assertIn(missing, str(stopped.exception.code))

    def test_dejavu_is_found_under_the_xdg_data_dirs(self):
        with tempfile.TemporaryDirectory() as top:
            data = os.path.join(top, "data")
            folder = os.path.join(data, "fonts", "truetype", "dejavu")
            os.makedirs(folder)
            font = os.path.join(folder, "DejaVuSans-Bold.ttf")
            open(font, "wb").close()
            if os.pathsep in data:
                self.skipTest("XDG_DATA_DIRS is split on %r; %s cannot be named in it" % (os.pathsep, data))
            with self.fake_home(top), mock.patch.dict(os.environ, {"XDG_DATA_DIRS": data}):
                namespace = make_icons_up_to_the_font([])
            self.assertEqual(namespace["BOLD"], font)

    def test_windows_fonts_folder_is_tried(self):
        with tempfile.TemporaryDirectory() as top:
            fonts = os.path.join(top, "Windows", "Fonts")
            os.makedirs(fonts)
            font = os.path.join(fonts, "arialbd.ttf")
            open(font, "wb").close()
            with self.fake_home(top):
                namespace = make_icons_up_to_the_font([])
            self.assertEqual(namespace["BOLD"], font)

    def test_no_font_found_says_where_it_looked(self):
        with tempfile.TemporaryDirectory() as top:
            with self.fake_home(top):
                namespace = make_icons_up_to_the_font(["--font", __file__])  # just to get the functions
                present = [path for path in namespace["font_candidates"]() if os.path.isfile(path)]
                if present:
                    self.skipTest("this host has %s, so a missing font cannot be shown" % present[0])
                looked = namespace["font_dirs"]() + namespace["font_candidates"]()
                with self.assertRaises(SystemExit) as stopped:
                    make_icons_up_to_the_font([])
            message = str(stopped.exception.code)
            self.assertIn("--font", message)
            self.assertIn("DejaVuSans-Bold.ttf", message)
            for place in looked:
                self.assertIn(place, message)
            self.assertIn(os.path.join(top, ".fonts"), looked)
            self.assertIn(os.path.join(top, "Windows", "Fonts", "arialbd.ttf"), looked)


if __name__ == "__main__":
    unittest.main()
