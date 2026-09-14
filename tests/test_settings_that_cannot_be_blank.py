"""Two ways the Settings page could write a value the daemon cannot use.

WHAT WENT WRONG

Found by sweeping every string field the dashboard offers, saving each one
empty, and reading back which ones were accepted. Twenty-eight were.

1. FOUR OF THEM ARE USED VERBATIM ON THE WIRE OR ON DISK. settings_file.REQUIRED
   stops NICKNAME, CHANNEL and ADMIN_NICK being cleared, because the daemon
   refuses to boot without them. Nothing stopped the rest, and REQUIRED is the
   wrong list to extend: it means "an operator must supply this", not "this
   cannot be empty".

     SERVER        -> s.connect((config.SERVER, config.PORT)) with no host
     ALT_NICKNAME  -> the literal line "NICK " after a 433, answered with 431
     LIST_BASE_NAME-> the master list's filename
     WEBUI_HOST    -> the interface the dashboard binds; empty is EVERY
                      interface, where the default is careful to say loopback

   The shipped default decides it, with no list to maintain. None means "unset
   unless you say otherwise" and blank is how an operator says it again -
   RAR_BINARY cleared is "look on PATH", DEBUG_CHANNEL ships blank because an
   install with no debug channel is not misconfigured. A non-empty shipped
   default means there is no code path for empty anywhere.

2. SCRIPT_VERSION WAS AN EDITABLE FIELD. It describes the code, not the
   install. Saving it wrote the number into settings.conf - which is applied
   AFTER the shipped defaults, so from then on the file won, permanently. The
   next upgrade shipped a new version and the daemon went on reporting the old
   one in the advert, the list masthead, the CTCP VERSION reply and the
   dashboard, with nothing to say why. "What version are you running?" is the
   first question asked about any install.

AND THE FALLBACK THAT WAS SUPPOSED TO CATCH THE FIRST ONE

    alt_nick = getattr(config, 'ALT_NICKNAME', f"{main_nick}`")

reads as a defence against a missing or empty alt nick and is not one:
getattr's default fires only when the attribute is ABSENT, and defaults.py
declares ALT_NICKNAME as a str with a real value, so it never is. Unreachable
in both of the two places it appears.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import irc  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheShippedDefaultDecidesWhetherBlankIsAllowed(DCCoreTestCase):
    """The property, stated once and applied to every setting there is. A list
    of names would go stale the first time somebody added a setting; this
    cannot."""

    def setUp(self):
        super().setUp()
        self.dir = self.make_tree().root
        self.path = os.path.join(self.dir, "settings.conf")
        with io.open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write("NICKNAME = Bot\nCHANNEL = #c\nADMIN_NICK = a\n")

    def save(self, name, value):
        return settings_file.save(vars(config), {name: value},
                                  path=self.path, log=lambda message: None)

    def test_no_setting_with_a_non_empty_shipped_default_can_be_blanked(self):
        shipped = getattr(config, "SHIPPED_VALUES", {})
        self.assertTrue(shipped, "defaults.SHIPPED_VALUES is missing - the rule "
                                 "below has nothing to consult")

        checked = 0
        for name, value in sorted(shipped.items()):
            if not isinstance(value, str) or not value.strip():
                continue
            if name == "ADMIN_PASSWORD_HASH":
                continue
            checked += 1
            with self.subTest(setting=name):
                with self.assertRaises(settings_file.SettingsWriteError):
                    self.save(name, "")

        self.assertGreater(checked, 10,
                           "almost nothing was checked - SHIPPED_VALUES is not "
                           "being populated the way this rule expects")

    def test_the_four_that_were_found_accepted(self):
        """Named explicitly as well as covered by the property above, because
        these are the ones a real sweep found and the reason this file exists."""
        for name in ("SERVER", "ALT_NICKNAME", "LIST_BASE_NAME", "WEBUI_HOST"):
            with self.subTest(setting=name):
                with self.assertRaises(settings_file.SettingsWriteError):
                    self.save(name, "")

    def test_whitespace_only_is_blank_too(self):
        for value in (" ", "\t", "   "):
            with self.subTest(value=value):
                with self.assertRaises(settings_file.SettingsWriteError):
                    self.save("SERVER", value)

    def test_a_setting_that_ships_blank_can_still_be_blanked(self):
        """DEBUG_CHANNEL ships as "" because an install with no debug channel is
        a real configuration, not a broken one. If this rule caught it, the
        setting could never be turned off again."""
        self.assertEqual(config.SHIPPED_VALUES.get("DEBUG_CHANNEL"), "")

        result = self.save("DEBUG_CHANNEL", "")

        self.assertIn("DEBUG_CHANNEL", result["written"])

    def test_a_setting_that_ships_as_none_can_still_be_blanked(self):
        """RAR_BINARY cleared means "look on PATH" - the documented way to
        undo an explicit path."""
        self.assertIsNone(config.SHIPPED_VALUES.get("RAR_BINARY"))

        result = self.save("RAR_BINARY", "")

        self.assertIn("RAR_BINARY", result["written"])

    def test_a_real_value_still_saves(self):
        """Control. The rule must only refuse blanks."""
        result = self.save("SERVER", "irc.example.org")

        self.assertIn("SERVER", result["written"])

    def test_the_shipped_default_is_consulted_not_the_current_one(self):
        """After one blank save the CURRENT value is empty, so a rule reading
        that would wave through every save after it. This is why the snapshot
        is taken in defaults.py before the overrides run."""
        self.set_config(SERVER="")

        with self.assertRaises(settings_file.SettingsWriteError):
            self.save("SERVER", "")


class ScriptVersionDescribesTheCodeNotTheInstall(DCCoreTestCase):

    def test_it_is_not_an_overridable_setting(self):
        self.assertFalse(
            settings_file.is_overridable("SCRIPT_VERSION", config.SCRIPT_VERSION))

    def test_it_is_not_offered_on_the_settings_page(self):
        payload = webserver.build_settings_payload()
        names = {field["name"] for category in payload["categories"]
                 for field in category["fields"]}

        self.assertNotIn("SCRIPT_VERSION", names)

    def test_saving_it_is_refused(self):
        tree = self.make_tree().root
        path = os.path.join(tree, "settings.conf")
        with io.open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("NICKNAME = Bot\n")

        with self.assertRaises(settings_file.SettingsWriteError):
            settings_file.save(vars(config), {"SCRIPT_VERSION": "DCCore v0.0.0"},
                               path=path, log=lambda message: None)

    def test_a_version_left_in_an_existing_file_is_ignored(self):
        """Files written before this change still have the line in them. It
        must not be applied - that is the pinning this fixes - and it must not
        stop the rest of the file being read either."""
        tree = self.make_tree().root
        path = os.path.join(tree, "settings.conf")
        with io.open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("SCRIPT_VERSION = DCCore v0.0.0-STALE\n"
                         "DEBUG_CHANNEL = #kept\n")

        namespace = {"SCRIPT_VERSION": "DCCore v9.9.9", "DEBUG_CHANNEL": "",
                     "__annotations__": {"SCRIPT_VERSION": str,
                                         "DEBUG_CHANNEL": str}}
        report = settings_file.apply_to(namespace, path=path,
                                        log=lambda message: None)

        self.assertEqual(namespace["SCRIPT_VERSION"], "DCCore v9.9.9")
        self.assertEqual(namespace["DEBUG_CHANNEL"], "#kept")
        self.assertIn("SCRIPT_VERSION", report["unknown"])

    def test_the_category_list_names_only_real_settings(self):
        """SETTINGS_CATEGORIES is not what decides whether a field renders -
        build_settings_payload() filters by declared_types + is_overridable, so
        a name left in a category after it stopped being a setting simply
        vanishes from the page and nothing says so. Removing SCRIPT_VERSION
        from the category was therefore invisible to every other test here;
        this is the assertion that makes the list mean something."""
        declared = settings_file.declared_types(vars(config))
        listed = [name for _id, _label, names in webserver.SETTINGS_CATEGORIES
                  for name in names]

        stale = [name for name in listed
                 if name != "ADMIN_PASSWORD_HASH"
                 and not (name in declared
                          and settings_file.is_overridable(
                              name, getattr(config, name, None)))]

        self.assertEqual(stale, [],
                         f"SETTINGS_CATEGORIES lists {stale}, which "
                         f"build_settings_payload() will silently drop")

    def test_project_url_is_still_a_setting(self):
        """Deliberately not excluded: a fork pointing at its own repository is
        a real configuration. It is only stopped from being blanked."""
        self.assertTrue(
            settings_file.is_overridable("PROJECT_URL", config.PROJECT_URL))


class TheAltNickFallbackActuallyRuns(DCCoreTestCase):

    def test_a_configured_alt_nick_is_used(self):
        self.set_config(ALT_NICKNAME="MyAlt")

        self.assertEqual(irc.resolve_alt_nick("MainNick"), "MyAlt")

    def test_a_blank_alt_nick_falls_back_instead_of_sending_nothing(self):
        """The defect: "NICK " with no nickname, which is answered with 431
        and leaves the bot with no nick at all on that connection."""
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                self.set_config(ALT_NICKNAME=blank)

                self.assertEqual(irc.resolve_alt_nick("MainNick"), "MainNick`")

    def test_the_wire_line_is_never_a_bare_nick_command(self):
        """Stated the way the server sees it, which is the thing that actually
        went wrong."""
        for blank in ("", "  ", None):
            with self.subTest(blank=blank):
                self.set_config(ALT_NICKNAME=blank)

                line = f"NICK {irc.resolve_alt_nick('MainNick')}"

                self.assertNotEqual(line.strip(), "NICK")
                self.assertTrue(line.split(None, 1)[1].strip())

    def test_surrounding_whitespace_is_removed(self):
        """A NICK parameter is space-delimited, so a padded value would end the
        command early."""
        self.set_config(ALT_NICKNAME="  Padded  ")

        self.assertEqual(irc.resolve_alt_nick("MainNick"), "Padded")


class BothCallSitesUseIt(unittest.TestCase):
    """Read out of the source: a helper that exists and is not called would
    satisfy every test above while both 433 handlers went on using the
    unreachable getattr default."""

    def test_no_getattr_alt_nickname_default_remains(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()

        # The assignment, not the name: resolve_alt_nick's own docstring quotes
        # the old line so the reason survives, and an assertion matching that
        # prose would pass on a file that had gone back to using it.
        self.assertNotIn("alt_nick = getattr(", source,
                         "a 433 handler is reading ALT_NICKNAME through getattr "
                         "again - its default cannot fire, because defaults.py "
                         "always defines the attribute")

    def test_both_handlers_call_the_helper(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertEqual(source.count("resolve_alt_nick("), 3,
                         "expected the definition plus both 433 call sites")


if __name__ == "__main__":
    unittest.main()
