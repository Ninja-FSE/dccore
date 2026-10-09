"""Search can be turned off (#1237).

The advert said "Search: ON" and the reply header "Search Result: ON", but both
were fixed text: every @find with a term was answered and no setting could
stop it. SEARCH_ENABLED (default on) now does: off, @find and @locator get no
reply and the advert says "Search: OFF".
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import settings_help  # noqa: E402
import theme  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402
from tests.test_webserver import write_master_list  # noqa: E402

NICK = "SomeBot"
USER = "someuser"
CHANNEL = "#somechannel"
FOLDERS = [(list_mod.LIST_FOLDER_PREFIX + "Rock\\Some Band\\1999 - Some Album\\",
            [("01-some_song.flac", "19.73MB"), ("02-other_song.flac", "20.00MB")])]


def advert(**settings):
    return announce.build_advert_line(CHANNEL, NICK, 10, "1.0GB", "2026-10-08",
                                      "3/3", 0, "0kB/s", "0kB/s", "0B", "DCCore",
                                      settings=settings or None)


class TheSwitch(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        write_master_list(self.tree.lists, NICK, FOLDERS)
        self.set_config(FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME=NICK, NICKNAME=NICK, CHANNEL=CHANNEL,
                        MAX_SEARCH_RESULTS=10, THEME="classic",
                        search_inprogress=False, update_inprogress=False)

    def reply(self, term="some_song"):
        self.oserve.queued.clear()
        out = io.StringIO()
        with redirect_stdout(out):
            list_mod.execute_search(RecordingSocket(), USER, term, CHANNEL)
        return [message for _u, message, *_ in self.oserve.queued], out.getvalue()

    def test_on_by_default(self):
        self.assertIs(config.SEARCH_ENABLED, True)

    def test_on_answers_as_before(self):
        lines, _out = self.reply()
        self.assertIn("Search Result:", lines[0])
        self.assertEqual(sum("::INFO::" in line for line in lines), 1)

    def test_off_sends_nothing(self):
        self.set_config(SEARCH_ENABLED=False)
        lines, out = self.reply()
        self.assertEqual(lines, [])
        self.assertIn("searching is off (SEARCH_ENABLED)", out)
        self.assertIn(USER, out)

    def test_off_sends_nothing_even_for_a_short_term(self):
        """The short-term notice would itself be an answer."""
        self.set_config(SEARCH_ENABLED=False)
        self.assertEqual(self.reply("ab")[0], [])

    def test_only_a_real_off_turns_it_off(self):
        for value in ("false", 0, None, ""):
            with self.subTest(value=value):
                self.set_config(SEARCH_ENABLED=value)
                self.assertTrue(self.reply()[0])

    def test_locator_takes_the_same_path(self):
        """#1249 review: grepping for the branch's own condition line only
        proves the TEXT "@locator" is matched somewhere - it says nothing
        about what runs once it is. Checked here instead: the branch body,
        up to the next elif, calls list.execute_search exactly once - so
        whatever "@find" does, "@locator" necessarily does the identical
        thing, by construction, not by two call sites that happen to agree
        today and could silently drift apart."""
        with open(os.path.join(REPO_ROOT, "src", "irc.py"), encoding="utf-8") as handle:
            source = handle.read()
        start = source.index('elif msg.startswith("@find ") or msg.startswith("@locator "):')
        end = source.index("elif ", start + 1)
        branch = source[start:end]
        self.assertEqual(branch.count("list.execute_search"), 1, branch)


class TheAdvert(DCCoreTestCase):

    def test_on_says_on_in_the_value_colour(self):
        value = theme.blocks()[5]
        self.assertIn("Search: " + value + "ON", advert())

    def test_off_says_off_in_the_alert_colour(self):
        self.set_config(SEARCH_ENABLED=False)
        alert = theme.blocks()[6]
        line = advert()
        self.assertIn("Search: " + alert + "OFF", line)
        self.assertNotIn("Search: " + theme.blocks()[5] + "ON", line)

    def test_the_preview_reads_the_unsaved_value_first(self):
        self.assertIn("OFF", advert(SEARCH_ENABLED=False))
        self.set_config(SEARCH_ENABLED=False)
        self.assertIn("ON", advert(SEARCH_ENABLED=True).split("Search: ")[1][:8])

    def test_the_real_preview_route_reads_the_unsaved_value_too(self):
        """#1249 review: the test above calls build_advert_line(settings=...)
        directly, which is not what the real preview route does - that one
        goes through theme_preview_overrides() first, to turn a posted body
        into the settings dict build_theme_preview() then renders. That
        whitelist stopped at THEME and CUSTOM_THEME_*, so a toggled-but-
        unsaved SEARCH_ENABLED never reached the preview at all; the advert
        sample kept showing the SAVED setting regardless of what was on the
        page, the one bug class this whole page of previews exists to
        prevent."""
        self.set_config(SEARCH_ENABLED=True)
        overrides = webserver.theme_preview_overrides({"SEARCH_ENABLED": False})
        preview = webserver.build_theme_preview(overrides)
        self.assertIn("OFF", preview["advert"])

        self.set_config(SEARCH_ENABLED=False)
        overrides = webserver.theme_preview_overrides({"SEARCH_ENABLED": True})
        preview = webserver.build_theme_preview(overrides)
        self.assertIn("ON", preview["advert"].split("Search: ")[1][:8])

    def test_the_preview_overrides_whitelist_coerces_to_a_real_boolean(self):
        """Whatever JSON type the body carries - and the dashboard now sends
        a real boolean, not the "true"/"false" strings THEME/CUSTOM_THEME_*
        use - this must not let a string "false" read as truthy."""
        self.assertEqual(webserver.theme_preview_overrides({"SEARCH_ENABLED": False}),
                         {"SEARCH_ENABLED": False})
        self.assertEqual(webserver.theme_preview_overrides({"SEARCH_ENABLED": True}),
                         {"SEARCH_ENABLED": True})
        self.assertEqual(webserver.theme_preview_overrides({}), {})

    def test_on_is_the_advert_it_always_was(self):
        """Byte for byte: the advert other scripts parse is unchanged when on."""
        BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()
        self.assertIn(f"{BG_TEXT_BOX} Search: {V}ON{R}{BG_TEXT_BOX} ", advert())


class TheSetting(unittest.TestCase):

    def test_it_is_on_the_settings_page_beside_the_search_cap(self):
        with open(os.path.join(REPO_ROOT, "src", "webserver.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"SEARCH_ENABLED", "MAX_SEARCH_RESULTS"', source)
        self.assertIn('"SEARCH_ENABLED": "Answer @find searches"', source)

    def test_it_has_help_and_labels_in_every_language(self):
        self.assertIn("SEARCH_ENABLED", settings_help.PLAIN_HELP)
        for lang in ("en", "es", "fr"):
            with open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"), encoding="utf-8") as handle:
                strings = json.load(handle)
            self.assertIn("settings.field.SEARCH_ENABLED", strings)
            if lang != "en":
                self.assertIn("settings.field.SEARCH_ENABLED.help", strings)

    def test_the_sample_settings_file_lists_it(self):
        with open(os.path.join(REPO_ROOT, "conf", "settings.conf.sample"), encoding="utf-8") as handle:
            self.assertIn("#SEARCH_ENABLED = true", handle.read())


if __name__ == "__main__":
    unittest.main()
