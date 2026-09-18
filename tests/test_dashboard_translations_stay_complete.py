"""The dashboard's translation dictionaries stay in step with each other
and with what the page actually references.

WHAT THIS GUARDS AGAINST

Three JSON files (web/lang/en.json, fr.json, es.json) and two source files
(web/index.html's data-i18n attributes, web/app.js's views{} object) all
have to agree on the same set of keys - and nothing enforces that by
construction. Four ways that drifts, each silent until an operator notices:

  * A key added to one language and forgotten in another. t() falls back to
    English for a viewer in that language, which is a real user staring at
    the wrong language on part of their own dashboard - not a crash, so
    nothing else would catch it.
  * A key referenced in HTML or app.js that no dictionary defines at all.
    t() then falls back to the literal key string - "view.search.title"
    printed on screen instead of "Search" - which is loud when it happens
    but invisible until it does, since nothing runs the page's JavaScript.
  * A key left in en.json after nothing references it any more - dead
    weight nobody notices removing, and a growing set of keys that need
    translating for no reason.
  * The files stop being valid JSON. web/lang/*.json ship as data, not
    code, so nothing else parses them before the browser does.

Deliberately not covering the Console or the debug channel: neither is
translated (see docs/UPDATES.md and issue #69) - both show the same lines
the daemon's own log does, which is a separate, larger piece of work.

ONE DELIBERATE EXCEPTION: a "settings.field.NAME.help" key (#528's "?"
tooltip text). fieldHelp() in app.js looks these up directly in the loaded
dictionary rather than through t(), specifically so a language missing one
falls back to the server's own English explanation rather than to a raw
key string - see the comment above fieldHelp() itself. That makes a
".help" key optional by design: en.json is never expected to carry one (the
server's text already is the English version), and fr.json/es.json filling
them in gradually, one setting at a time, is the intended, expected state -
not drift. The cross-language completeness check below excludes them for
that reason; SettingsHelpKeysAreHonest below checks the one thing that
still matters for them - that a ".help" key actually names a real setting,
so a typo in a translated key does not just silently never match.
"""

import io
import json
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

WEB = os.path.join(REPO_ROOT, "web")
LANG_DIR = os.path.join(WEB, "lang")
LANGUAGES = ("en", "fr", "es")

DATA_I18N = re.compile(r'data-i18n(?:-html|-placeholder|-title)?="([^"]+)"')
# A translation key looks like "namespace.name" or "namespace.sub.name" -
# lowercase-led dotted segments. app.js uses these three ways that a single
# fixed regex cannot all catch at once: literal t("key") calls, an object
# literal's values (DOWNLOAD_STATE_LABELS, STATUS_LABELS, and the settings
# category/field label overrides - all looked up dynamically by an id the
# server sent, then passed to t()), and a key assigned to a local variable
# through a ternary before being passed to t(theVariable) (the broadcast
# view's soFarKey/doneKey). Matching the shape directly, anywhere in the
# file, covers all three without three separate regexes to keep in sync
# with how app.js happens to phrase each one today.
#
# Segments allow an underscore as well as letters/digits: the settings
# lookups above end each key in the exact SCREAMING_SNAKE_CASE name the
# server uses (SETTINGS_LABELS in webserver.py - "settings.field.MAX_DCC_SLOTS"),
# on purpose, so the two stay directly cross-referenceable. A narrower
# class here would silently stop matching those keys, the same way it once
# stopped one character short of "settings.field.SERVER"'s neighbours.
JS_KEY_SHAPED_STRING = re.compile(r'"([a-z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)+)"')

HELP_KEY_SUFFIX = ".help"


def is_settings_help_key(key):
    """A "?" tooltip translation (see the module docstring) - optional in
    every language, including English, by design."""
    return key.endswith(HELP_KEY_SUFFIX)


def read(name):
    with io.open(os.path.join(WEB, name), encoding="utf-8") as handle:
        return handle.read()


def load_dict(code):
    with io.open(os.path.join(LANG_DIR, code + ".json"), encoding="utf-8") as handle:
        return json.load(handle)


def keys_referenced_in_source():
    """Every translation key the dashboard's own source actually asks for -
    index.html's data-i18n, data-i18n-html, data-i18n-placeholder and
    data-i18n-title attributes, plus every key-shaped string literal in
    app.js (see JS_KEY_SHAPED_STRING above for why a shape match, not a
    narrower t(...)-call match, is what covers all of app.js's ways of
    naming a key)."""
    html = read("index.html")
    found = set(DATA_I18N.findall(html))

    js = read("app.js")
    found.update(JS_KEY_SHAPED_STRING.findall(js))

    return found


class TheDictionariesAreValidJSON(unittest.TestCase):

    def test_each_language_file_parses(self):
        for code in LANGUAGES:
            with self.subTest(language=code):
                load_dict(code)  # raises if this is not valid JSON


class TheDictionariesAgreeWithEachOther(unittest.TestCase):
    """Same keys everywhere, so a viewer in any of the three languages sees
    that language on every string this page currently translates - never a
    silent drop back to English for one language and not the others."""

    def test_no_language_is_missing_a_key_another_one_has(self):
        key_sets = {code: set(k for k in load_dict(code).keys()
                              if not is_settings_help_key(k))
                    for code in LANGUAGES}
        union = set().union(*key_sets.values())

        for code, keys in key_sets.items():
            missing = sorted(union - keys)
            with self.subTest(language=code):
                self.assertEqual(missing, [],
                                 f"{code}.json is missing keys the other "
                                 f"languages define: {missing}")

    def test_no_translated_value_is_blank(self):
        """A key present but empty reads as "translated to nothing" rather
        than "not translated yet" - the one case a missing-key fallback
        cannot catch, since the key IS there."""
        for code in LANGUAGES:
            values = load_dict(code)
            blank = sorted(k for k, v in values.items() if not str(v).strip())
            with self.subTest(language=code):
                self.assertEqual(blank, [],
                                 f"{code}.json has blank values for: {blank}")


class EveryKeyTheSourceUsesExists(unittest.TestCase):
    """The other direction: a key the page actually asks for has to be
    defined, or t() falls back to printing the raw key on screen."""

    def test_every_referenced_key_is_in_english(self):
        english = load_dict("en")
        referenced = keys_referenced_in_source()

        missing = sorted(referenced - set(english.keys()))
        self.assertEqual(missing, [],
                         "referenced in index.html or app.js but not in "
                         "web/lang/en.json: " + ", ".join(missing))

    def test_the_scan_actually_finds_keys(self):
        """Fixture invariant. A regex or a moved views{} block that stopped
        matching would make the test above pass by finding nothing to check,
        on any tree, forever."""
        self.assertGreater(len(keys_referenced_in_source()), 15)


class NothingInEnglishGoesUnused(unittest.TestCase):
    """The direction the other guards do not cover: a key nobody references
    is dead weight that still has to be kept translated in fr.json and
    es.json for no reason. Not a fault on its own, but worth knowing about
    before it accumulates."""

    def test_every_english_key_is_referenced_somewhere(self):
        english = load_dict("en")
        referenced = keys_referenced_in_source()

        orphaned = sorted(set(english.keys()) - referenced)
        self.assertEqual(orphaned, [],
                         "defined in web/lang/en.json but not referenced by "
                         "index.html's data-i18n attributes or app.js's "
                         "views{} object: " + ", ".join(orphaned))


class SettingsHelpKeysAreHonest(unittest.TestCase):
    """The one thing that still has to hold for the optional ".help" keys
    (see the module docstring): each one's stem - the part before ".help" -
    must be a real "settings.field.NAME" key, i.e. one en.json defines and
    the page actually references. Without this, a typo in a translated key
    (a wrong NAME, a doubled suffix) would just never match in fieldHelp()
    and silently fall back to English, exactly like a key nobody defined at
    all - the one failure mode this file otherwise always catches."""

    def test_every_help_key_names_a_real_settings_field(self):
        english = set(load_dict("en").keys())
        referenced = keys_referenced_in_source()

        for code in LANGUAGES:
            help_keys = [k for k in load_dict(code).keys() if is_settings_help_key(k)]
            with self.subTest(language=code):
                bad = sorted(k for k in help_keys
                             if k[:-len(HELP_KEY_SUFFIX)] not in english
                             or k[:-len(HELP_KEY_SUFFIX)] not in referenced)
                self.assertEqual(bad, [],
                                 f"{code}.json has \".help\" keys that do not "
                                 f"name a real, referenced settings field: {bad}")


if __name__ == "__main__":
    unittest.main()
