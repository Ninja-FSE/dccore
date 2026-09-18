"""#528, part two: "a lot of settings are not easy to understand" - a "?"
beside every setting on the dashboard, with the explanation on hover.

The explanation already existed. Every setting in defaults.py carries a
comment block, and scripts/gen_settings_sample.py has parsed those into
settings.conf.sample since the sample was first generated. So the parser
moved into settings_help.py, the generator imports it, and /api/settings
sends the same text as `help` per field. One source, three readers - the
code, the sample and the page - and none of them can drift from the others.

Twenty settings had no comment at all (PORT, CHANNEL, the DCC port range,
five of the six theme roles...). They have one now, and the guard here
keeps it that way: a new setting without an explanation fails the build
rather than shipping as a "?" with nothing behind it.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import settings_file  # noqa: E402
import settings_help  # noqa: E402


class TheParser(unittest.TestCase):
    """settings_help.parse_help() on small sources, so each rule is pinned
    where it is cheap to read."""

    def test_the_block_above_a_setting(self):
        found = settings_help.parse_help(
            "# How many at once.\n# Three is plenty.\nMAX: int = 3\n")

        self.assertEqual(found["MAX"], ["How many at once.", "Three is plenty."])

    def test_the_inline_comment_after_the_value(self):
        found = settings_help.parse_help("PORT: int = 6667   # The server's port\n")

        self.assertEqual(found["PORT"], ["The server's port"])

    def test_block_and_inline_together_in_that_order(self):
        found = settings_help.parse_help("# Above.\nX = 1  # beside\n")

        self.assertEqual(found["X"], ["Above.", "beside"])

    def test_a_hash_inside_the_value_is_not_a_comment(self):
        """The generator's own old bug: CHANNEL = "#one,#two" split on the
        first '#' and produced a junk comment line."""
        found = settings_help.parse_help('CHANNEL: str = "#one,#two"  # where to serve\n')

        self.assertEqual(found["CHANNEL"], ["where to serve"])

    def test_a_section_rule_ends_the_block(self):
        source = "# 1. NETWORK\n# ------\n# The server.\nSERVER = 'x'\n"

        self.assertEqual(settings_help.parse_help(source)["SERVER"], ["The server."])

    def test_a_setting_with_nothing_has_an_empty_list(self):
        self.assertEqual(settings_help.parse_help("BARE = 1\n")["BARE"], [])

    def test_plain_and_annotated_assignments_both_count(self):
        found = settings_help.parse_help("# a\nA = 1\n# b\nB: int = 2\n")

        self.assertEqual(set(found), {"A", "B"})

    def test_a_multi_line_value_takes_no_inline_comment(self):
        """The inline rule reads the tail of the FIRST line; on a value that
        spans lines that tail is the opening of the value, not a comment."""
        found = settings_help.parse_help("# Long.\nTEXT = (\n    'a'  # not this\n)\n")

        self.assertEqual(found["TEXT"], ["Long."])


class TheTextForThePage(unittest.TestCase):
    """developer_text() joins the comment lines into paragraphs: a blank
    comment line is a paragraph break, everything else runs on."""

    def setUp(self):
        self._real = settings_help.help_lines
        self.addCleanup(setattr, settings_help, "help_lines", self._real)

    def fake(self, lines):
        settings_help.help_lines = lambda: {"X": lines}

    def test_lines_join_with_spaces(self):
        self.fake(["one", "two"])
        self.assertEqual(settings_help.developer_text("X"), "one two")

    def test_a_blank_line_is_a_paragraph_break(self):
        self.fake(["one", "", "two"])
        self.assertEqual(settings_help.developer_text("X"), "one\n\ntwo")

    def test_leading_and_trailing_blanks_do_not_make_empty_paragraphs(self):
        self.fake(["", "one", ""])
        self.assertEqual(settings_help.developer_text("X"), "one")

    def test_nothing_is_the_empty_string(self):
        self.fake([])
        self.assertEqual(settings_help.developer_text("X"), "")
        self.assertEqual(settings_help.developer_text("NOT_THERE"), "")


class TheRealFile(unittest.TestCase):
    """Against defaults.py as committed."""

    def overridable(self):
        return [name for name in settings_help.help_lines()
                if hasattr(config, name)
                and settings_file.is_overridable(name, getattr(config, name))]

    def test_every_overridable_setting_has_an_explanation(self):
        """The guard. A setting an operator can change from the dashboard
        without a word beside it in defaults.py is a "?" with nothing behind
        it - and a settings.conf.sample line with no comment above it."""
        silent = [name for name in self.overridable() if not settings_help.help_text(name)]

        self.assertEqual(silent, [],
                         "these settings have no comment block or inline "
                         "comment in defaults.py; write one - it is what the "
                         "dashboard's '?' and settings.conf.sample both show: "
                         + ", ".join(silent))

    def test_the_scan_sees_the_settings(self):
        """Fixture invariant: an empty scan would pass the guard above."""
        self.assertGreater(len(self.overridable()), 100)

    def test_the_cache_follows_the_file(self):
        """Re-parsed when defaults.py changes, not once per process - a
        rehash after an edit must show the new text."""
        import tempfile
        real_path = settings_help.DEFAULTS_PATH
        real_cache = dict(settings_help._cache)
        self.addCleanup(setattr, settings_help, "DEFAULTS_PATH", real_path)
        self.addCleanup(settings_help._cache.update, real_cache)
        fd, path = tempfile.mkstemp(suffix=".py")
        os.close(fd)
        self.addCleanup(os.remove, path)
        settings_help.DEFAULTS_PATH = path
        settings_help._cache["stamp"] = None

        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("# first\nX = 1\n")
        self.assertEqual(settings_help.help_text("X"), "first")

        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("# second, and longer than the first so the size differs\nX = 1\n")
        self.assertEqual(settings_help.help_text("X"),
                         "second, and longer than the first so the size differs")

    def test_an_unreadable_file_means_no_help_not_a_crash(self):
        real_path = settings_help.DEFAULTS_PATH
        self.addCleanup(setattr, settings_help, "DEFAULTS_PATH", real_path)
        # Built from a temp dir that is removed first, so the name is one
        # that provably does not exist - and not a literal in the source,
        # which tests/test_referenced_files_exist.py would read as a file
        # somebody forgot to ship.
        import tempfile
        gone = tempfile.mkdtemp(prefix="dccore-no-defaults-")
        os.rmdir(gone)
        settings_help.DEFAULTS_PATH = os.path.join(gone, "defaults.py")

        self.assertEqual(settings_help.developer_text("MAX_DCC_SLOTS"), "")
        # The plain explanation does not come from that file, so the page
        # still has something to show.
        self.assertTrue(settings_help.help_text("MAX_DCC_SLOTS"))


class ItIsWrittenForTheOperator(unittest.TestCase):
    """The first "?" showed the developer's comment block, and NICKNAME's
    talks about settings_file.REQUIRED and oserve.startup() - right for the
    code, meaningless for an operator. "How is a simple user going to
    understand it?" So every setting the page shows has an explanation
    written for the operator, and the developer's block stays where
    developers read it.
    """

    def page_settings(self):
        import webserver
        return [n for _id, _label, names in webserver.SETTINGS_CATEGORIES for n in names]

    def test_every_setting_on_the_page_has_a_plain_explanation(self):
        missing = [n for n in self.page_settings() if not settings_help.plain_text(n)]
        self.assertEqual(missing, [], "write a PLAIN_HELP entry in settings_help.py for: " + ", ".join(missing))

    def test_no_plain_explanation_is_for_a_setting_that_does_not_exist(self):
        stray = sorted(set(settings_help.PLAIN_HELP) - set(self.page_settings()))
        self.assertEqual(stray, [], "PLAIN_HELP entries for settings the page does not show: " + ", ".join(stray))

    def test_the_plain_text_wins_over_the_developer_block(self):
        self.assertEqual(settings_help.help_text("NICKNAME"), settings_help.plain_text("NICKNAME"))
        self.assertNotIn("settings_file", settings_help.help_text("NICKNAME"))
        self.assertNotIn("oserve", settings_help.help_text("NICKNAME"))

    def test_it_speaks_no_code(self):
        """The rule that makes it plain: no module or file names, no
        function calls, no issue numbers, no ALL_CAPS settings other than
        the ones an operator has to type. A few settings must be named
        because the operator sees them on the same page."""
        import re
        jargon = re.compile(r"\.py\b|\w+\(\)|#\d{2,}|settings_file|oserve\b|webserver\b|announce\b|dcc\.|list\.py")
        # admin_config.py is a file the operator edits by hand; naming it
        # is instruction, not jargon.
        offenders = {n: t for n, t in settings_help.PLAIN_HELP.items()
                     if jargon.search(t.replace("admin_config.py", ""))}
        self.assertEqual(offenders, {})

    def test_it_is_short(self):
        """A tooltip, not an essay: three sentences at most in practice."""
        long = {n: len(t) for n, t in settings_help.PLAIN_HELP.items() if len(t) > 320}
        self.assertEqual(long, {})

    def test_the_sample_shows_the_plain_text_first(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        self.addCleanup(lambda: sys.path.remove(os.path.join(REPO_ROOT, "scripts")))
        import gen_settings_sample
        sample = gen_settings_sample.build()
        plain = settings_help.plain_text("NICKNAME")
        first_words = " ".join(plain.split()[:6])
        at = sample.index(first_words)
        self.assertLess(at, sample.index("#NICKNAME = "))
        # and the developer's block still follows it, for whoever wants it
        self.assertIn("settings_file.REQUIRED", sample[at:sample.index("#NICKNAME = ")])

    def test_french_and_spanish_carry_a_translation_of_every_one(self):
        """The tooltip follows the language picker: fieldHelp() looks up
        settings.field.<NAME>.help before falling back to this English."""
        for lang in ("fr", "es"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"),
                         encoding="utf-8") as handle:
                d = json.load(handle)
            for name, english in settings_help.PLAIN_HELP.items():
                key = "settings.field." + name + ".help"
                with self.subTest(lang=lang, setting=name):
                    self.assertIn(key, d)
                    self.assertNotEqual(d[key], english, "not translated")

    def test_english_is_not_duplicated_in_the_dictionary(self):
        """One source. en.json carries no .help: the page falls back to the
        server's text, which is PLAIN_HELP - so there is nothing to drift."""
        with io.open(os.path.join(REPO_ROOT, "web", "lang", "en.json"), encoding="utf-8") as handle:
            d = json.load(handle)
        self.assertEqual([k for k in d if k.endswith(".help")], [])


class TheGeneratorUsesTheSameParser(unittest.TestCase):
    """One source. The generator must not keep a copy of the parser that
    could drift from the one the page reads."""

    def test_gen_settings_sample_imports_it(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "gen_settings_sample.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("import settings_help", source)
        self.assertNotIn("def _doc_lines(", source,
                         "the generator has its own copy of the parser again")
        self.assertNotIn("def assignment_parts(", source)


class ThePayloadCarriesIt(unittest.TestCase):
    def fields(self):
        import webserver
        payload = webserver.build_settings_payload()
        return {f["name"]: f for c in payload["categories"] for f in c["fields"]}

    def test_every_field_has_help(self):
        fields = self.fields()
        self.assertGreater(len(fields), 50)
        without = sorted(name for name, f in fields.items() if not f.get("help"))
        self.assertEqual(without, [], "fields sent to the page with no help: " + ", ".join(without))

    def test_the_help_is_the_plain_explanation(self):
        fields = self.fields()
        self.assertEqual(fields["MAX_DCC_SLOTS"]["help"],
                         settings_help.plain_text("MAX_DCC_SLOTS"))
        self.assertIn("at the same time", fields["MAX_DCC_SLOTS"]["help"])


class ThePageDrawsIt(unittest.TestCase):
    """Read from the source, as the other dashboard tests are."""

    def app_js(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_label_is_followed_by_the_help_mark(self):
        source = self.app_js()
        row = source[source.index("function settingsFieldHtml("):]
        row = row[:row.index("function settingsPasswordSectionHtml(")]

        self.assertIn("escapeHtml(fieldLabel(field)) +\n      settingsHelpHtml(field)", row,
                      "the '?' is not rendered right after the label")

    def test_the_help_text_is_text_content_never_an_attribute(self):
        """escapeHtml() encodes text, not attributes: a help line with a
        quote in it must not be able to close one."""
        source = self.app_js()
        block = source[source.index("function settingsHelpHtml("):]
        block = block[:block.index("function settingsFieldHtml(")]

        self.assertIn('<span class="settings-help-text" role="tooltip">\' + escapeHtml(help)', block)
        self.assertNotIn("title=", block)
        self.assertNotIn("data-help=", block)

    def test_a_field_without_help_gets_no_mark(self):
        source = self.app_js()
        block = source[source.index("function settingsHelpHtml("):]
        block = block[:block.index("function settingsFieldHtml(")]

        self.assertIn('if (!help) { return ""; }', block)

    def test_a_translation_wins_over_the_servers_english(self):
        source = self.app_js()
        block = source[source.index("function fieldHelp("):]
        block = block[:block.index("function settingsHelpHtml(")]

        self.assertIn('state.lang[key + ".help"]', block)
        self.assertIn("return field.help", block)

    def test_the_mark_is_reachable_by_keyboard_and_labelled(self):
        source = self.app_js()
        self.assertIn('class="settings-help" tabindex="0"', source)
        self.assertIn('t("settings.whatThisDoes")', source)

    def test_the_label_key_exists_in_every_dictionary(self):
        for lang in ("en", "fr", "es"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"),
                         encoding="utf-8") as handle:
                self.assertIn("settings.whatThisDoes", json.load(handle), lang)

    def test_the_css_shows_it_on_hover_and_on_focus(self):
        with io.open(os.path.join(REPO_ROOT, "web", "style.css"), encoding="utf-8") as handle:
            css = handle.read()

        self.assertIn(".settings-help:focus .settings-help-text", css,
                      "keyboard users cannot open the help")
        self.assertIn(".settings-help:hover .settings-help-text", css)


if __name__ == "__main__":
    unittest.main()
