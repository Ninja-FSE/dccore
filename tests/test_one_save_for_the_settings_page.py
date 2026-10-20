"""Settings has one Save, and it saves the folder list too (#1357).

The folder rows and the list editor had Save buttons of their own, and the
page's Save & rehash saved only the settings: an operator who removed folders
and pressed it saved nothing they had changed there, and one who pressed Save
folders found Save & rehash disabled with nothing left for it to do. Now the
page's button saves the editor first and the settings after, and a refused
folder list stops it before the settings.

The page's own functions run under node, with a fake server that records what
was posted. Every folder path and name here is invented.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests import support  # noqa: F401  (path setup)

REPO_ROOT = support.REPO_ROOT

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

function classes() {
  const set = {};
  return { toggle: function (name, on) { set[name] = !!on; }, has: function (name) { return !!set[name]; } };
}
const el = {
  settingsSaveBtn: { disabled: false },
  settingsSaveStatus: { style: {} },
  settingsRestartNote: { style: {} },
  settingsSavebarText: { textContent: "", classList: classes() },
  settingsFields: { querySelector: function () { return null; }, querySelectorAll: function () { return []; } }
};

let state = {};
let posted = [];
let answers = {};
let confirmAnswer = true;
const shown = [];
function postJson(url, body) {
  posted.push(url);
  return Promise.resolve(answers[url]);
}
function t(key) { return key; }
function renderSettingsCategory() {}
function refreshThemePreview() {}
function loadSettings() {}
function loadLists() { return Promise.resolve(); }
function showSettingsStatus(node, text, isError) { shown.push({ text: text, error: !!isError }); }
// #1462's wait for the rehash, which an accepted settings save starts: its own
// tests are in test_the_dashboard_says_whether_a_save_took_effect.py. Without
// it here the save threw inside its .then() and said "could not reach".
const awaited = [];
function awaitSettingsRehash(saved) { awaited.push(saved); }
const window = { confirm: function () { return confirmAnswer; } };

const NAMES = ["draftFolderShape", "draftListShape", "servedDraftUnsaved", "updateSettingsSaveBar",
               "saveFolders", "saveLists", "saveSettingsPage", "saveTheSettings"];
const page = new Function("state", "el", "t", "postJson", "renderSettingsCategory", "refreshThemePreview",
                          "loadSettings", "loadLists", "showSettingsStatus", "window", "awaitSettingsRehash",
  NAMES.map(function (name) { return fn("function " + name + "("); }).join("\n") +
  "\nreturn {" + NAMES.map(function (n) { return n + ": " + n; }).join(", ") + "};");

const MUSIC = { name: "Music", path: "X:\\Music" };
const SHELF = { name: "Shelf", path: "X:\\Shelf" };
const OK_FOLDERS = { ok: true, status: 200, data: { folders: [SHELF], source: "file", written: 1 } };
const OK_SETTINGS = { ok: true, status: 200, data: { written: ["LIST_BASE_NAME"], restart_required: [] } };

function fresh(extra) {
  state = Object.assign({ folders: [MUSIC], foldersDraft: [MUSIC], foldersSource: "file_directory",
                          listsSource: "", lists: [], listsDraft: null, settingsDirty: {},
                          settingsBaseline: {}, settingsActiveCategory: "your-list" }, extra || {});
  posted = [];
  shown.length = 0;
  awaited.length = 0;
  answers = { "/api/folders": OK_FOLDERS, "/api/settings": OK_SETTINGS,
              "/api/lists": { ok: true, status: 200, data: { lists: [], message: "Saved." } } };
  return page(state, el, t, postJson, renderSettingsCategory, refreshThemePreview,
              loadSettings, loadLists, showSettingsStatus, window, awaitSettingsRehash);
}
function settle() { return new Promise(function (resolve) { setTimeout(resolve, 0); }); }

(async function () {
  const out = {};
  let p;

  // Nothing edited: nothing is posted.
  p = fresh();
  p.updateSettingsSaveBar();
  out.nothing = { disabled: el.settingsSaveBtn.disabled };
  p.saveSettingsPage();
  await settle();
  out.nothing.posted = posted.slice();

  // The operator's case: the Music directory replaced by a shelf, nothing else.
  p = fresh({ foldersDraft: [SHELF] });
  p.updateSettingsSaveBar();
  out.foldersOnly = { disabledBefore: el.settingsSaveBtn.disabled };
  p.saveSettingsPage();
  await settle();
  Object.assign(out.foldersOnly, { posted: posted.slice(), shown: shown.slice(),
                                   unsavedAfter: p.servedDraftUnsaved(),
                                   bar: el.settingsSavebarText.textContent,
                                   disabledAfter: el.settingsSaveBtn.disabled });

  // Folders and a setting: the folders first, then the settings.
  p = fresh({ foldersDraft: [SHELF], settingsDirty: { LIST_BASE_NAME: "Shelves" } });
  p.saveSettingsPage();
  await settle();
  out.both = { posted: posted.slice(), dirtyAfter: state.settingsDirty, shown: shown.slice(),
               awaited: awaited.slice() };

  // A refused folder list stops the save before the settings.
  p = fresh({ foldersDraft: [], settingsDirty: { LIST_BASE_NAME: "Shelves" } });
  answers["/api/folders"] = { ok: false, status: 400,
                              data: { error: "Keep at least one folder: a bot with no folders has nothing to share." } };
  p.saveSettingsPage();
  await settle();
  out.refused = { posted: posted.slice(), note: state.foldersNote, dirtyAfter: state.settingsDirty,
                  disabledAfter: el.settingsSaveBtn.disabled };

  // The list editor goes the same way.
  p = fresh({ listsSource: "file", lists: [{ name: "Main", primary: true, channels: [], folders: [SHELF] }],
              listsDraft: [{ name: "Main", primary: true, channels: [], folders: [MUSIC] }],
              settingsDirty: { LIST_BASE_NAME: "Shelves" } });
  p.saveSettingsPage();
  await settle();
  out.lists = { posted: posted.slice(), shown: shown.slice(), awaited: awaited.slice() };

  // A refused list stops the save before the settings too.
  p = fresh({ listsSource: "file", lists: [{ name: "Main", primary: true, channels: [], folders: [SHELF] }],
              listsDraft: [{ name: "", primary: true, channels: [], folders: [MUSIC] }],
              settingsDirty: { LIST_BASE_NAME: "Shelves" } });
  answers["/api/lists"] = { ok: false, status: 400, data: { error: "A list needs a name." } };
  p.saveSettingsPage();
  await settle();
  out.listsRefused = { posted: posted.slice(), note: state.listsNote, dirtyAfter: state.settingsDirty };

  // Clearing the debug channel asks first; "no" saves nothing at all.
  p = fresh({ foldersDraft: [SHELF], settingsDirty: { DEBUG_CHANNEL: "" },
              settingsBaseline: { DEBUG_CHANNEL: "#somewhere" } });
  confirmAnswer = false;
  p.saveSettingsPage();
  await settle();
  out.declined = { posted: posted.slice() };
  confirmAnswer = true;

  console.log(JSON.stringify(out));
})().catch(function (err) { console.error(err && err.stack || err); process.exit(1); });
"""

_RESULT = []


def run_the_page():
    if not _RESULT:
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
        _RESULT.append(json.loads(done.stdout.decode("utf-8")))
    return _RESULT[0]


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class TheOneSave(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_the_page()

    def test_with_nothing_edited_there_is_nothing_to_save(self):
        self.assertEqual(self.result["nothing"], {"disabled": True, "posted": []})

    def test_a_folder_edit_alone_is_saved_by_it(self):
        got = self.result["foldersOnly"]
        self.assertFalse(got["disabledBefore"], "an edited folder list must be savable from the page's button")
        self.assertEqual(got["posted"], ["/api/folders"])
        self.assertEqual(got["unsavedAfter"], "")
        self.assertEqual(got["bar"], "settings.allChangesSaved")
        self.assertTrue(got["disabledAfter"])
        # What the folder save says - rebuild to publish - reaches the bar's status.
        self.assertEqual(got["shown"], [{"text": "settings.savedOneFolder", "error": False}])

    def test_folders_go_first_then_the_settings(self):
        got = self.result["both"]
        self.assertEqual(got["posted"], ["/api/folders", "/api/settings"])
        self.assertEqual(got["dirtyAfter"], {})
        # the settings save went through to its end: no error said, and it
        # waits for the rehash it started
        self.assertEqual([s for s in got["shown"] if s["error"]], [])
        self.assertEqual(got["awaited"], [{"written": ["LIST_BASE_NAME"], "restart_required": []}])

    def test_a_refused_folder_list_keeps_the_settings_unsaved(self):
        got = self.result["refused"]
        self.assertEqual(got["posted"], ["/api/folders"])
        self.assertFalse(got["note"]["ok"])
        self.assertIn("at least one folder", got["note"]["text"])
        self.assertEqual(got["dirtyAfter"], {"LIST_BASE_NAME": "Shelves"})
        self.assertFalse(got["disabledAfter"], "the operator can fix it and save again")

    def test_the_list_editor_is_saved_the_same_way(self):
        got = self.result["lists"]
        self.assertEqual(got["posted"], ["/api/lists", "/api/settings"])
        self.assertEqual([s for s in got["shown"] if s["error"]], [])
        self.assertEqual(len(got["awaited"]), 1)

    def test_a_refused_list_keeps_the_settings_unsaved(self):
        got = self.result["listsRefused"]
        self.assertEqual(got["posted"], ["/api/lists"])
        self.assertFalse(got["note"]["ok"])
        self.assertEqual(got["dirtyAfter"], {"LIST_BASE_NAME": "Shelves"})

    def test_declining_the_debug_channel_question_saves_nothing(self):
        self.assertEqual(self.result["declined"]["posted"], [])


class TheEditorsHaveNoSaveOfTheirOwn(unittest.TestCase):
    """A second button that saves half the page is what caused #1354."""

    def test_no_save_button_is_drawn_in_either_editor(self):
        with open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            code = handle.read()
        self.assertEqual(re.findall(r"served-(?:folder|list)-save", code), [])

    def test_the_page_button_runs_the_one_save(self):
        with open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            code = handle.read()
        self.assertEqual(code.count('el.settingsSaveBtn.addEventListener("click", saveSettingsPage);'), 1)


if __name__ == "__main__":
    unittest.main()
