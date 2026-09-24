"""The "?" help for the seven size settings said "in bytes" while the
field beside it is typed in MB or KB (audit L22, #686).

settings_help.PLAIN_HELP is one text for two readers: settings.conf.sample,
where the value IS bytes, and the dashboard's "?", where SETTINGS_UNITS
renders MAX_RAR_FOLDER_SIZE, MAX_FETCH_FILE_SIZE, MAX_LIST_TEXT_SIZE,
MAX_FETCH_FOLDER_FILE_SIZE and MAX_FETCH_LIST_FILE_SIZE in MB and
DCC_SEND_BUFFER and LIST_HEADER_MAX_BYTES in KB. An operator who hovered
"Max folder size to pack", read "in bytes" and typed 10737418240 into the
MB box set a 10 PB limit. The page's settingsHelpHtml() now appends, for a
field with a unit, "On this page the value is typed and shown in {unit};
the file keeps bytes." - in the page's own language - after the shared
text. The shared text is untouched, so the sample stays right.

Asserted through node: the rendering function is lifted out of app.js and
run with the page's real dictionaries, so the property is the tooltip's
text, not the shape of the source.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import webserver  # noqa: E402

NODE = shutil.which("node")


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def lifted(name, source):
    """The source of one top-level `function name(...) { ... }` in app.js."""
    start = source.index("  function %s(" % name)
    depth, i = 0, source.index("{", start)
    while True:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1


@unittest.skipUnless(NODE, "no node on PATH")
class TheTooltipInARealEngine(unittest.TestCase):
    """One node PROCESS for the whole class (#898), not one per render.

    The class used to start a fresh node for every case - about ten a run,
    each its own 30s timeout - so each was a separate chance to lose to a
    cold start; it did, once, on a slow Windows runner (TimeoutExpired,
    333s overall). Nothing about what is asserted changes: the same
    functions lifted from app.js, the same dictionaries, the same regex
    pulling the tooltip text out of the rendered span - only how many
    times a process is spawned to get there.
    """

    RENDERS = None       # {key: raw settingsHelpHtml() output}, filled once

    @classmethod
    def _render_all(cls):
        """Every (lang, field) case any test method below needs, in one
        script, one process. `state` is declared once and then reassigned
        per language - a free global to the lifted functions either way,
        so this changes nothing about what they see per call."""
        app = read("web", "app.js")
        en = read("web", "lang", "en.json")
        cases = [("en", name, webserver._settings_field(name, int, 0))
                 for name in webserver.SETTINGS_UNITS]
        cases.append(("es", "MAX_RAR_FOLDER_SIZE",
                     webserver._settings_field("MAX_RAR_FOLDER_SIZE", int, 0)))
        cases.append(("fr", "MAX_RAR_FOLDER_SIZE",
                     webserver._settings_field("MAX_RAR_FOLDER_SIZE", int, 0)))
        cases.append(("en", "MAX_DCC_SLOTS",
                     webserver._settings_field("MAX_DCC_SLOTS", int, 3)))

        lines = [
            "var SETTINGS_FIELD_LABEL_KEYS = {};",
            "function escapeHtml(s) { return String(s); }",
            lifted("t", app), lifted("fieldHelp", app), lifted("settingsHelpHtml", app),
            "var state = {};",
            "var results = {};",
        ]
        loaded_langs = {}
        for index, (lang, name, field) in enumerate(cases):
            if lang not in loaded_langs:
                loaded_langs[lang] = read("web", "lang", lang + ".json") if lang != "en" else en
                lines.append("state = { lang: %s, langFallback: %s };"
                             % (loaded_langs[lang], en))
            lines.append("results[%s] = settingsHelpHtml(%s);"
                         % (json.dumps("%d:%s:%s" % (index, lang, name)), json.dumps(field)))
        lines.append("process.stdout.write(JSON.stringify(results));")
        script = "\n".join(lines)

        # A file, not `node -e`: with every language's dictionary inlined
        # the script is well past what a Windows command line can carry.
        import tempfile
        path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
                path = handle.name
                handle.write(script)
            # 120s for the whole class in one process - the other real-engine
            # test (test_the_dashboard_javascript_parses_in_a_real_engine.py)
            # uses 60s for a single `node --check`; this does real work ten
            # times over, so it gets twice that rather than the same figure
            # split ten ways.
            done = subprocess.run([NODE, path], capture_output=True, text=True,
                                  encoding="utf-8", timeout=120)
        finally:
            if path:
                os.remove(path)
        if done.returncode != 0:
            raise AssertionError("node failed: " + done.stderr)
        raw = json.loads(done.stdout)
        return {(lang, name): raw["%d:%s:%s" % (index, lang, name)]
                for index, (lang, name, _field) in enumerate(cases)}

    @classmethod
    def setUpClass(cls):
        cls.RENDERS = cls._render_all()

    def tooltip(self, lang, name):
        html = self.RENDERS[(lang, name)]
        match = re.search(r'role="tooltip">(.*?)</span>', html, re.S)
        return match.group(1) if match else ""

    def test_every_size_field_says_which_unit_the_page_uses(self):
        for name, (unit, _factor) in webserver.SETTINGS_UNITS.items():
            text = self.tooltip("en", name)

            self.assertIn("in bytes", text, name)  # the shared text, still right for the file
            self.assertTrue(text.endswith("On this page the value is typed and shown in %s; the file keeps bytes." % unit),
                            (name, text))

    def test_in_spanish_and_french_too(self):
        self.assertIn("En esta página el valor se escribe y se muestra en MB",
                      self.tooltip("es", "MAX_RAR_FOLDER_SIZE"))
        self.assertIn("Sur cette page la valeur se saisit et s'affiche en MB",
                      self.tooltip("fr", "MAX_RAR_FOLDER_SIZE"))

    def test_a_field_without_a_unit_gets_nothing_added(self):
        field = webserver._settings_field("MAX_DCC_SLOTS", int, 3)
        text = self.tooltip("en", "MAX_DCC_SLOTS")

        self.assertNotIn("On this page the value", text)
        self.assertEqual(text, field["help"])


class TheSharedTextIsUntouched(unittest.TestCase):

    def test_the_sample_still_says_bytes_for_every_size(self):
        import settings_help
        for name in webserver.SETTINGS_UNITS:
            self.assertIn("in bytes", settings_help.PLAIN_HELP[name], name)

    def test_every_language_carries_the_sentence_with_the_placeholder(self):
        for lang in ("en", "es", "fr"):
            strings = json.loads(read("web", "lang", lang + ".json"))
            self.assertIn("{unit}", strings["settings.help.shownIn"], lang)


if __name__ == "__main__":
    unittest.main()
