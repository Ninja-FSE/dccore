"""The dashboard's JSON answers keep their keys in the order they were built.

#1143: create_app() left Flask's JSON provider at its default, sort_keys=True,
so every dict in every answer was sorted key by key before it went out -
most of the serialisation cost of /api/fetch/status, which the Downloads
view polls every four seconds with up to 1,500 rows. Nothing reads the keys
in sorted order: the page looks fields up by name, json.loads gives the same
dict either way, and the mIRC script talks over DCC CHAT, not HTTP. The one
place the page listed a payload's keys - the KeepTrack preview's "skipped"
reasons - now sorts them itself, so it reads exactly as before.

ensure_ascii is deliberately left on. With it off, a filename os.listdir()
handed back as a lone surrogate (an undecodable name on Linux, an unpaired
UTF-16 one on Windows) goes out raw, Werkzeug cannot encode the body, and the
whole route answers 500 - the audit's skeptic reproduced it. That half of the
proposal was dropped, and a test here keeps it dropped.

Flask is supported from 2.0 (requirements-web.txt). app.json, the provider
object, arrived in 2.2; 2.0 and 2.1 read app.config["JSON_SORT_KEYS"]
instead, so the setting goes wherever the installed Flask looks for it.
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests import support  # noqa: F401  (path setup)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402
from tests.test_webserver import WEBUI_TEST_PASSWORD, log_in_test_client  # noqa: E402

REPO_ROOT = support.REPO_ROOT


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class TheRealRoute(support.DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(
            WEBUI_TEST_PASSWORD, iterations=1000))
        self.client = webserver.create_app().test_client()
        log_in_test_client(self.client)

    def put(self, filename):
        # Built "state" before "bot" on purpose: sorted, "bot" would lead.
        config.fetch_queue["rid"] = {
            "state": "failed", "bot": "SomeBot", "filename": filename,
            "request_type": "file", "requested_at": 1.0, "reason": "gone",
        }

    def test_the_keys_go_out_in_the_order_they_were_built(self):
        self.put("Song.flac")
        resp = self.client.get("/api/fetch/status")
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        self.assertLess(text.index('"state"'), text.index('"bot"'))
        self.assertLess(text.index('"reason"'), text.index('"id"'))

    def test_what_the_page_decodes_is_unchanged(self):
        self.put("Song.flac")
        rows = self.client.get("/api/fetch/status").get_json()
        self.assertEqual(rows, [{
            "state": "failed", "bot": "SomeBot", "filename": "Song.flac",
            "request_type": "file", "requested_at": 1.0, "reason": "gone", "id": "rid",
        }])

    def test_an_undecodable_filename_still_gets_an_answer(self):
        """ensure_ascii stays on: a lone surrogate is escaped as \\udcXX
        instead of breaking the encoder and the whole route with it."""
        odd = "caf" + chr(0xDCE9) + ".flac"
        self.put(odd)
        resp = self.client.get("/api/fetch/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()[0]["filename"], odd)
        resp.get_data().decode("ascii")

    def test_non_ascii_names_are_still_escaped(self):
        greek = "".join(chr(code) for code in (0x3B1, 0x3B2, 0x3B3)) + ".flac"
        self.put(greek)
        resp = self.client.get("/api/fetch/status")
        resp.get_data().decode("ascii")
        self.assertEqual(resp.get_json()[0]["filename"], greek)


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class EveryFlaskThisSupports(unittest.TestCase):
    """The installed Flask is one version; the floor is 2.0. Both ways of
    turning sorting off are exercised against stand-ins for each."""

    def test_a_flask_with_a_json_provider_has_sorting_turned_off_there(self):
        class Provider(object):
            sort_keys = True
            ensure_ascii = True

        class App(object):
            def __init__(self):
                self.json = Provider()
                self.config = {}

        app = App()
        webserver._keep_json_key_order(app)
        self.assertIs(app.json.sort_keys, False)
        self.assertIs(app.json.ensure_ascii, True)
        self.assertEqual(app.config, {})

    def test_a_flask_before_2_2_has_it_turned_off_in_the_config(self):
        class App(object):
            def __init__(self):
                self.config = {"JSON_SORT_KEYS": True}

        app = App()
        webserver._keep_json_key_order(app)
        self.assertIs(app.config["JSON_SORT_KEYS"], False)
        self.assertNotIn("JSON_AS_ASCII", app.config)

    def test_the_real_app_is_configured_whichever_flask_is_installed(self):
        app = webserver.create_app()
        provider = getattr(app, "json", None)
        if provider is not None and hasattr(provider, "sort_keys"):
            self.assertIs(provider.sort_keys, False)
            self.assertIs(provider.ensure_ascii, True)
        else:
            self.assertIs(app.config["JSON_SORT_KEYS"], False)


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const NL = String.fromCharCode(10);
const fn = (name) => {
  const i = src.indexOf("  function " + name + "(");
  const j = src.indexOf(NL + "  }" + NL, i);
  if (i < 0 || j < 0) { throw new Error("missing " + name); }
  return src.slice(i, j + 4);
};
const now = fn("ktdataLine") + NL + fn("renderKTDataPreview");
// The page as it was before #1143, which listed the keys as they came.
const was = now.replace("var reasons = Object.keys(skipped).sort();",
                        "var reasons = Object.keys(skipped);");
if (was === now) { throw new Error("could not rebuild the old preview"); }
function render(code, payload) {
  const lines = [];
  const el = { ktdataPreview: { innerHTML: "", hidden: true,
                                appendChild: (node) => { lines.push(node.textContent); } },
               ktdataConfirm: { hidden: true } };
  const document = { createElement: () => ({ textContent: "" }) };
  const t = (k) => k === "stats.ktdataSkipped" ? "{count}|{reasons}" : k;
  new Function("el", "document", "t", code + NL + "renderKTDataPreview(arguments[3]);")
    (el, document, t, payload);
  return lines;
}
const reasons = ["not five tab-separated fields", "a count out of range", "not a nick",
                 "neither Sent nor Received", "a count that is not a whole number"];
const counts = { };
reasons.forEach((r, i) => { counts[r] = i + 1; });
const built = {}; reasons.forEach((r) => { built[r] = counts[r]; });
const sorted = {}; reasons.slice().sort().forEach((r) => { sorted[r] = counts[r]; });
const figures = { sent: { nicks: 1, files: 2 }, received: { nicks: 0, files: 0 } };
console.log(JSON.stringify({
  now: render(now, { figures: figures, skipped: built }),
  was: render(was, { figures: figures, skipped: sorted }),
  wasUnsorted: render(was, { figures: figures, skipped: built }),
}));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class ThePreviewUnderNode(unittest.TestCase):
    """The real renderKTDataPreview() out of app.js, fed the skipped reasons
    in the order the server now builds them."""

    def test_it_reads_as_it_did_when_the_server_sorted_the_keys(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        seen = json.loads(done.stdout.decode("utf-8"))
        self.assertEqual(seen["now"], seen["was"])
        # And the order really was at stake: unsorted input reads differently.
        self.assertNotEqual(seen["wasUnsorted"], seen["was"])
        self.assertIn("15|a count out of range (2), a count that is not a whole number (5),",
                      seen["now"][1])


class ThePageSortsTheOneListItShows(unittest.TestCase):
    """The KeepTrack preview printed payload.skipped's reasons in key order,
    which was alphabetical only because the server sorted it."""

    def test_the_skipped_reasons_are_sorted_on_the_page(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        body = js.split("  function renderKTDataPreview(payload) {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("var reasons = Object.keys(skipped).sort();", body)
        self.assertNotIn("var reasons = Object.keys(skipped);", body)


if __name__ == "__main__":
    unittest.main()
