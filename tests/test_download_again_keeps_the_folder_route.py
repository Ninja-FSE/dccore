"""Download again asks for a failed folder by the folder route (#1040).

A folder fetch is asked for as "!<bot> !rar <folder>" and comes back as a pack
the other bot names itself - matched to a "folder" row by bot alone. Asking
again for a failed one went through the plain file enqueue, from the mIRC
Downloads window (`dlagain`) and the dashboard's Download again alike: the same
words went out, but as a FILE row named "!rar <folder>", and the pack that came
back matched nothing and was refused as unsolicited.

The dashboard half runs the real redownloadFetchRow() under node, and is
skipped where node is not installed; a source guard runs everywhere.
"""

import io
import os
import shutil
import subprocess
import tempfile
import unittest

from tests import support  # noqa: F401  (path setup)

import adminchat  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_dccore_downloads_window as window  # noqa: E402

REPO_ROOT = support.REPO_ROOT


class TheFolderItAskedFor(unittest.TestCase):
    def test_the_rar_word_in_front_is_dropped(self):
        self.assertEqual(dcc_fetch.folder_asked_for({"requested_filename": "!rar Artist - Album"}),
                         "Artist - Album")
        self.assertEqual(dcc_fetch.folder_asked_for({"requested_filename": "!RAR  Artist"}), "Artist")
        self.assertEqual(dcc_fetch.folder_asked_for({"requested_filename": "Artist - Album"}),
                         "Artist - Album")


class DlagainInTheMircWindow(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        self.session = window.make_session(self)
        persist = dcc_fetch.persist_fetch_history
        dcc_fetch.persist_fetch_history = lambda: None
        self.addCleanup(setattr, dcc_fetch, "persist_fetch_history", persist)

    def test_a_failed_folder_is_asked_for_by_the_folder_route(self):
        config.fetch_queue["aaaaaaaaaaaa"] = window.row(
            "failed", name="!rar Artist - Album", request_type="folder", reason="no response")
        asked, as_files = [], []
        real_folder, real_file = webserver.build_folder_rar_fetch_enqueue_result, webserver.build_fetch_enqueue_result
        webserver.build_folder_rar_fetch_enqueue_result = lambda bot, folder: (asked.append((bot, folder)) or (200, {}))
        webserver.build_fetch_enqueue_result = lambda items: (as_files.append(items) or (200, {}))
        self.addCleanup(setattr, webserver, "build_folder_rar_fetch_enqueue_result", real_folder)
        self.addCleanup(setattr, webserver, "build_fetch_enqueue_result", real_file)
        adminchat._cmd_dlagain(self.session, "aaaaaaaaaaaa")
        self.assertEqual(asked, [("SomeBot", "Artist - Album")])
        self.assertEqual(as_files, [], "never as a file")


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function fn(signature) {
  const start = src.indexOf(signature);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") { depth++; }
    else if (src[i] === "}") { depth--; if (depth === 0) { break; } }
  }
  return src.slice(start, i + 1);
}
const posted = [];
const state = { downloads: [
  { id: "f1", bot: "SomeBot", request_type: "folder", requested_filename: "!rar Artist - Album",
    filename: "Artist_-_Album.rar", state: "failed" },
  { id: "p1", bot: "SomeBot", request_type: "file", requested_filename: "Track.flac", state: "failed" },
] };
const redo = new Function("state", "postJson", "loadDownloads", "window",
  fn("function redownloadFetchRow(") + "\nreturn redownloadFetchRow;")(
  state, function (url, body) { posted.push([url, body]); return Promise.resolve({ ok: true }); },
  function () {}, { alert: function () {} });
redo({ dataset: { requestId: "f1" }, disabled: false });
redo({ dataset: { requestId: "p1" }, disabled: false });
console.log(JSON.stringify(posted));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class DownloadAgainOnTheDashboard(unittest.TestCase):
    def test_a_folder_goes_to_the_folder_route_and_a_file_to_the_file_one(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        import json
        self.assertEqual(json.loads(done.stdout.decode("utf-8")), [
            ["/api/filelists/fetch-folder-rar", {"bot": "SomeBot", "folder": "Artist - Album"}],
            ["/api/fetch/enqueue", [{"bot": "SomeBot", "filename": "Track.flac"}]],
        ])


class TheSourceSaysSo(unittest.TestCase):
    def test_both_send_a_folder_row_by_the_folder_route(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        self.assertIn('again = postJson("/api/filelists/fetch-folder-rar", { bot: row.bot, folder: folder });', js)
        with io.open(os.path.join(REPO_ROOT, "src", "adminchat.py"), encoding="utf-8") as handle:
            py = handle.read()
        self.assertIn("webserver.build_folder_rar_fetch_enqueue_result(bot, dcc_fetch.folder_asked_for(row))", py)


if __name__ == "__main__":
    unittest.main()
