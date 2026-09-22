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

    def render(self, lang, field):
        app = read("web", "app.js")
        script = "\n".join([
            "var SETTINGS_FIELD_LABEL_KEYS = {};",
            "var state = { lang: %s, langFallback: %s };" % (read("web", "lang", lang + ".json"), read("web", "lang", "en.json")),
            "function escapeHtml(s) { return String(s); }",
            lifted("t", app), lifted("fieldHelp", app), lifted("settingsHelpHtml", app),
            "process.stdout.write(settingsHelpHtml(%s));" % json.dumps(field),
        ])
        # A file, not `node -e`: with two dictionaries inlined the script is
        # past what a Windows command line can carry.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(script)
        self.addCleanup(os.remove, handle.name)
        done = subprocess.run([NODE, handle.name], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        tooltip = re.search(r'role="tooltip">(.*?)</span>', done.stdout, re.S)
        return tooltip.group(1) if tooltip else ""

    def test_every_size_field_says_which_unit_the_page_uses(self):
        for name, (unit, _factor) in webserver.SETTINGS_UNITS.items():
            field = webserver._settings_field(name, int, 0)
            text = self.render("en", field)

            self.assertIn("in bytes", text, name)  # the shared text, still right for the file
            self.assertTrue(text.endswith("On this page the value is typed and shown in %s; the file keeps bytes." % unit),
                            (name, text))

    def test_in_spanish_and_french_too(self):
        field = webserver._settings_field("MAX_RAR_FOLDER_SIZE", int, 0)

        self.assertIn("En esta página el valor se escribe y se muestra en MB", self.render("es", field))
        self.assertIn("Sur cette page la valeur se saisit et s'affiche en MB", self.render("fr", field))

    def test_a_field_without_a_unit_gets_nothing_added(self):
        field = webserver._settings_field("MAX_DCC_SLOTS", int, 3)
        text = self.render("en", field)

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
