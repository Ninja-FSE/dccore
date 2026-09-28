"""The version line and the Stats cards follow the language (#976).

The dashboard asks for the version check before its language file. When the
check answered first, renderVersion() drew t("version.upToDate") with no
dictionary loaded yet - the key itself - and #version-text has no data-i18n, so
applyTranslations() never put it right: the sidebar read "version.upToDate"
until the next poll, ten minutes on. A language switch left that line, and the
Stats cards built by renderLibrary(), in the old language the same way.

The behaviour test runs the real t(), renderVersion() and loadLanguage() under
node, and is skipped where node is not installed; the source guards below run
everywhere.
"""

import io
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
        return handle.read()


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function fn(signature) {
  const start = src.indexOf(signature);
  if (start < 0) { throw new Error("missing: " + signature); }
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") { depth++; }
    else if (src[i] === "}") { depth--; if (depth === 0) { break; } }
  }
  return src.slice(start, i + 1);
}
const code = ["function t(", "function renderVersion(", "function loadLanguage("].map(fn).join("\n");
const state = { lang: {}, langFallback: {}, lastVersionInfo: null, lastStats: null };
const versionText = { textContent: "", classList: { add() {}, remove() {} }, appendChild() {} };
const el = { versionText: versionText };
const dictionaries = {
  "/lang/en.json": { "version.upToDate": "{version} - up to date" },
  "/lang/fr.json": { "version.upToDate": "{version} - à jour" },
};
function fetchJson(url) { return Promise.resolve(dictionaries[url] || {}); }
const statsDrawn = [];
const page = new Function("state", "el", "fetchJson", "applyTranslations", "renderStats",
  "var langGeneration = 0;\n" + code + "\nreturn { renderVersion: renderVersion, loadLanguage: loadLanguage };")(
  state, el, fetchJson, function () {}, function (data) { statsDrawn.push(data); });

const out = [];
// The check answers before the language file.
page.renderVersion({ current: "v1.13.2", checked_at: 1 });
out.push("before=" + versionText.textContent);
state.lastStats = { library: {} };
page.loadLanguage("en").then(function () {
  out.push("after=" + versionText.textContent);
  out.push("stats=" + statsDrawn.length);
  return page.loadLanguage("fr");
}).then(function () {
  out.push("switched=" + versionText.textContent);
  console.log(out.join("\n"));
});
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class ItIsDrawnAgain(unittest.TestCase):

    def seen(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        return dict(line.split("=", 1) for line in done.stdout.decode("utf-8").splitlines())

    def test_the_key_it_showed_first_is_replaced(self):
        seen = self.seen()
        self.assertEqual(seen["before"], "version.upToDate", "the premise: no dictionary yet")
        self.assertEqual(seen["after"], "v1.13.2 - up to date")

    def test_a_language_switch_redraws_it(self):
        self.assertEqual(self.seen()["switched"], "v1.13.2 - à jour")

    def test_the_stats_are_drawn_again_too(self):
        self.assertEqual(self.seen()["stats"], "1")


class TheSourceSaysSo(unittest.TestCase):
    """What the node test checks, readable without node."""

    def test_what_was_drawn_is_kept_and_redrawn(self):
        js = app_js()
        self.assertIn("state.lastVersionInfo = info;", js)
        self.assertIn("state.lastStats = data;", js)
        start = js.index("function loadLanguage(")
        body = js[start:js.index("function chooseLanguage(", start)]
        after = body[body.index("applyTranslations();"):]
        self.assertIn("renderVersion(state.lastVersionInfo)", after)
        self.assertIn("renderStats(state.lastStats)", after)


if __name__ == "__main__":
    unittest.main()
