"""#1264: dccore.mrc's settings window is generated, and cannot drift.

scripts/mirc/build_settings_window.py writes the `dialog dccore.set` table and
its lookup data into a marked block of dccore.mrc, from the bot's own settings
metadata and scripts/mirc/settings_window_layout.py. These tests hold the two
promises that make that worth having:

  - the block in the committed dccore.mrc is exactly what the generator writes
    today, so a setting added, relabelled or given a unit on the bot side fails
    here until somebody runs the script;
  - every setting the dashboard's Settings page offers has a place in the
    window, exactly one, or is in the layout's EXCLUDED with a reason.

Each guard is mutation-checked below: a key placed twice, a key dropped, a
stale block and a check box too narrow for its label are each refused.
"""

import copy
import io
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIRC_DIR = os.path.join(REPO_ROOT, "scripts", "mirc")
for path in (os.path.join(REPO_ROOT, "src"), MIRC_DIR, os.path.join(REPO_ROOT, "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import build_settings_window as generator  # noqa: E402
import settings_window_layout as layout  # noqa: E402
import webserver  # noqa: E402


def layout_copy(**changes):
    """A stand-in for the layout module with some of its data replaced."""
    fake = types.SimpleNamespace(**{name: copy.deepcopy(getattr(layout, name))
                                    for name in ("GROUPS", "EXCLUDED", "FOLDER_KEYS", "FILE_KEYS",
                                                 "CONFIRM_PAGES")})
    for name, value in changes.items():
        setattr(fake, name, value)
    return fake


def placed_keys(groups):
    """Every setting key the layout places, once per placing."""
    keys = []
    for _group, pages in groups:
        for _page, sections in pages:
            for _header, items in sections:
                for item in items:
                    if isinstance(item, str):
                        keys.append(item)
                    elif "key" in item:
                        keys.append(item["key"])
                    elif "widget" in item:
                        keys.extend(item.get("keys", ()))
    return keys


def with_groups(edit):
    groups = [[group, [[page, [[header, list(items)] for header, items in sections]]
                       for page, sections in pages]] for group, pages in layout.GROUPS]
    edit(groups)
    return groups


class TheBlockIsUpToDate(unittest.TestCase):

    def test_check_passes_on_the_committed_script(self):
        out = io.StringIO()
        with redirect_stdout(out):
            status = generator.main(["--check"])
        self.assertEqual(status, 0, out.getvalue())

    def test_a_script_on_another_drive_is_named_as_it_is(self):
        # A CI Windows runner has the repository on D: and TEMP on C:, and
        # relpath() raises across drives; the rewrite above then failed
        # there alone. Faked here so it fails on every machine.
        def other_drive(path, start=None):
            raise ValueError("path is on mount 'C:', start on mount 'D:'")
        real = generator.os.path.relpath
        generator.os.path.relpath = other_drive
        self.addCleanup(setattr, generator.os.path, "relpath", real)
        self.assertEqual(generator.shown_path("C:/elsewhere/dccore.mrc"), "C:/elsewhere/dccore.mrc")

    def test_check_fails_on_a_stale_block_and_the_rewrite_fixes_it(self):
        """Mutation check of the guard above: one changed line in the block of
        a copy of the script, and --check says so; running the script puts it
        right."""
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        stale = os.path.join(folder, "dccore.mrc")
        with io.open(generator.SCRIPT, encoding="ascii", newline="") as handle:
            text = handle.read()
        self.assertIn('  title "DCCore - Settings"', text)
        with io.open(stale, "w", encoding="ascii", newline="") as handle:
            handle.write(text.replace('  title "DCCore - Settings"', '  title "DCCore - Old"', 1))
        original = generator.SCRIPT
        generator.SCRIPT = stale
        self.addCleanup(setattr, generator, "SCRIPT", original)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(generator.main(["--check"]), 1)
            self.assertEqual(generator.main([]), 0)
            self.assertEqual(generator.main(["--check"]), 0)
        with io.open(stale, encoding="ascii", newline="") as handle:
            self.assertEqual(handle.read(), text)

    def test_the_block_keeps_the_scripts_crlf_line_ends(self):
        with io.open(generator.SCRIPT, "rb") as handle:
            data = handle.read()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))

    def test_the_block_is_marked_once_and_in_order(self):
        with io.open(generator.SCRIPT, encoding="ascii", newline="") as handle:
            text = handle.read()
        self.assertEqual(text.count(generator.BEGIN), 1)
        self.assertEqual(text.count(generator.END), 1)
        self.assertLess(text.index(generator.BEGIN), text.index(generator.END))

    def test_the_generator_reads_no_setting_value(self):
        """The tri-state answer comes from defaults.py's declaration, not from
        a value a local settings.conf may have changed: the block is the same
        on every machine."""
        self.assertIn("WEBUI_CONSOLE_ENABLED", generator.tri_state_keys())
        self.assertNotIn("WEBUI_ENABLED", generator.tri_state_keys())


class EverySettingHasAPlace(unittest.TestCase):

    def every_key(self):
        return [key for _cid, _label, keys in webserver.SETTINGS_CATEGORIES for key in keys]

    def test_every_setting_is_placed_once_or_excluded_with_a_reason(self):
        placed = placed_keys(layout.GROUPS)
        for key in self.every_key():
            with self.subTest(key=key):
                if key in layout.EXCLUDED:
                    self.assertNotIn(key, placed)
                    self.assertGreater(len(layout.EXCLUDED[key].strip()), 20)
                else:
                    self.assertEqual(placed.count(key), 1)

    def test_nothing_is_placed_that_the_settings_page_does_not_offer(self):
        every = set(self.every_key())
        self.assertEqual([key for key in placed_keys(layout.GROUPS) if key not in every], [])

    def test_the_mockups_most_used_switches_are_on_general_settings(self):
        general = dict(dict(layout.GROUPS)["General"])["General Settings"]
        keys = placed_keys([("General", [("General Settings", general)])])
        for key in ("SEARCH_ENABLED", "ANNOUNCE_TRANSFERS", "RAR_ENABLED", "PRIVATE_MESSAGES_ENABLED",
                    "WEBUI_ENABLED", "CTCP_VERSION_REPLY", "CHECK_FOR_UPDATES"):
            self.assertIn(key, keys)
        search = dict(dict(layout.GROUPS)["Sharing"])["Search"]
        self.assertNotIn("SEARCH_ENABLED", placed_keys([("Sharing", [("Search", search)])]))

    def test_the_tabs_and_pages_are_the_mockups(self):
        self.assertEqual([group for group, _pages in layout.GROUPS],
                         ["General", "Sharing", "Downloads", "Security", "Dashboard & Console", "Advanced"])
        pages = {group: [page for page, _sections in pages] for group, pages in layout.GROUPS}
        self.assertEqual(pages["General"], ["IRC Server", "General Settings", "Channels", "Operator",
                                            "Appearance", "Advertising"])
        self.assertEqual(pages["Sharing"], ["Search", "Transfers", "Your list", "Lists & channels", "Rebuild"])
        self.assertEqual(pages["Downloads"], ["List discovery", "Fetch tuning", "Queue"])
        self.assertEqual(pages["Security"], ["Anti-flood", "Bans & ignores", "Private messages",
                                             "Admin console"])
        self.assertEqual(pages["Dashboard & Console"], ["Web dashboard", "Console feed", "This mIRC window"])
        self.assertEqual(pages["Advanced"], ["Debug & logging", "File locations"])


class TheGeneratorRefusesABadLayout(unittest.TestCase):
    """Mutation checks: the generator itself refuses what the tests above
    would catch, so a bad layout never reaches dccore.mrc."""

    def build(self, fake):
        return generator.Builder(fake, generator.metadata()).build()

    def test_the_real_layout_builds(self):
        self.build(layout_copy())

    def test_a_key_placed_twice_is_refused(self):
        def twice(groups):
            groups[0][1][3][1][0][1].append("SERVER")       # SERVER on Operator too
        with self.assertRaisesRegex(generator.LayoutError, "SERVER is placed twice"):
            self.build(layout_copy(GROUPS=with_groups(twice)))

    def test_a_key_with_no_place_is_refused(self):
        def drop(groups):
            groups[0][1][0][1][0][1].remove("PORT")
        with self.assertRaisesRegex(generator.LayoutError, "PORT"):
            self.build(layout_copy(GROUPS=with_groups(drop)))

    def test_a_key_both_placed_and_excluded_is_refused(self):
        with self.assertRaisesRegex(generator.LayoutError, "placed and excluded"):
            self.build(layout_copy(EXCLUDED={"SERVER": "a reason long enough to count as one"}))

    def test_a_check_too_long_for_its_column_is_refused(self):
        def longer(groups):
            groups[0][1][1][1][2][1].append({"local": "x", "label": "A check box label " * 6})
        with self.assertRaisesRegex(generator.LayoutError, "does not fit"):
            self.build(layout_copy(GROUPS=with_groups(longer)))

    def test_a_page_that_cannot_fit_is_refused(self):
        def crowd(groups):
            notes = [{"note": "A long note that takes room. " * 8} for _ in range(12)]
            groups[0][1][3][1].append(["Crowd", notes])
        with self.assertRaisesRegex(generator.LayoutError, "does not fit|cannot take two columns"):
            self.build(layout_copy(GROUPS=with_groups(crowd)))


class TheMetadataIsTheBots(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.block, cls.builder = generator.generate()
        cls.text = "\n".join(cls.block)

    def test_the_labels_are_the_dashboards_with_their_units(self):
        self.assertIn('text "%s", ' % webserver.SETTINGS_LABELS["MAX_DCC_SLOTS"], self.text)
        label, _factor = webserver.SETTINGS_UNITS["MAX_FETCH_FILE_SIZE"]
        self.assertIn('text "%s (%s)", ' % (webserver.SETTINGS_LABELS["MAX_FETCH_FILE_SIZE"], label), self.text)
        self.assertRegex(self.text, r"hadd dccore\.swm k\.MAX_FETCH_FILE_SIZE \d+ int %d \d+"
                         % webserver.SETTINGS_UNITS["MAX_FETCH_FILE_SIZE"][1])

    def test_the_choices_are_settings_file_choices(self):
        import settings_file
        for key, choices in settings_file.CHOICES.items():
            self.assertIn("hadd dccore.swm ch.%s %s" % (key, " ".join(choices)), self.text)
        self.assertIn("hadd dccore.swm cl.DCC_BLOCK_SIZE.1 4 KB", self.text)

    def test_the_help_is_what_the_settings_page_shows(self):
        import settings_help
        line = next(line for line in self.block if line.startswith("  hadd dccore.swm h.MAX_DCC_SLOTS "))
        shown = untext(line.split(" ", 5)[5])
        self.assertTrue(settings_help.help_text("MAX_DCC_SLOTS").startswith(shown.rstrip(".")), shown)

    def test_the_width_table_is_the_options_dialogs(self):
        from test_the_options_dialog_labels_fit_their_controls import CHAR_PX
        self.assertEqual(generator.CHAR_PX, CHAR_PX)

    def test_every_hash_line_is_safe_to_run(self):
        """A generated hadd line carries no $ or % (mIRC would evaluate them),
        no | (a command separator), no braces or brackets and no #: the text
        has them as ~HH. (A comma is safe in a command: the id lists use them.)"""
        checked = 0
        for line in self.block:
            if line.startswith("  hadd dccore.swm "):
                self.assertNotRegex(line, r"[$%|{}\[\]#]", line)
                checked += 1
        self.assertGreater(checked, 500)

    def test_every_line_is_well_under_mircs_limit(self):
        self.assertLess(max(len(line) for line in self.block), 900)


def untext(text):
    """$dccore.sw.untext, in Python: ~HH is the character HH."""
    return re.sub(r"~([0-9A-F][0-9A-F])", lambda m: chr(int(m.group(1), 16)), text)


if __name__ == "__main__":
    unittest.main()
