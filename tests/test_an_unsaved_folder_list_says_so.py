"""Settings > Your list says when the folder or list editor is not saved (#1354).

An operator removed all twenty served folders, set the Music directory and
clicked Save & rehash. Only Save folders writes the folder list, so the next
rebuild scanned all twenty - and the page had said "Serving 0 folders" (it
counted the editor's draft) and "All changes saved" (the save bar watched only
the settings). Now the summary counts what is saved, a note beside the editor's
Save button says it is not saved yet, and the save bar says so too.

The page's own functions run under node, against a fake panel.
Every folder path and name here is invented.
"""

import json
import os
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

function fakeInput() {
  const listeners = {};
  return {
    value: "",
    addEventListener: function (kind, handler) { (listeners[kind] = listeners[kind] || []).push(handler); },
    type: function (text) { this.value = text; (listeners.input || []).forEach(function (h) { h(); }); }
  };
}

function classes() {
  const set = {};
  return { toggle: function (name, on) { set[name] = !!on; }, has: function (name) { return !!set[name]; } };
}

// The panel: the unsaved note, and the folder rows attachFolderRows() fills.
let rows = [];
const note = { textContent: "", hidden: true };
const el = {
  settingsSaveBtn: { disabled: false },
  settingsSavebarText: { textContent: "", classList: classes() },
  settingsFields: {
    querySelector: function (selector) { return selector === ".served-draft-unsaved" ? note : null; },
    querySelectorAll: function (selector) { return selector === ".served-folder-row" ? rows : []; }
  }
};
function rowFor(index) {
  const name = fakeInput(), path = fakeInput();
  return { dataset: { servedFolderIndex: String(index) },
           querySelector: function (s) { return s === ".served-folder-name" ? name : path; },
           name: name, path: path };
}

let state = {};
let answer = null;
function postJson(url, body) { return { then: function (callback) { callback(answer); } }; }
// The counted strings keep their {count}, so a test sees the number as well
// as which sentence was chosen.
function t(key) { return /Folders?$|NotSaved(One|Many)$|UnsavedChanges?$/.test(key) ? key + " {count}" : key; }
function escapeHtml(text) { return String(text); }
function renderSettingsCategory() {}
function refreshThemePreview() {}

const NAMES = ["draftFolderShape", "draftListShape", "servedDraftUnsaved", "servedDraftNoteText",
               "refreshServedDraftNote", "updateSettingsSaveBar", "foldersSectionHtml",
               "attachFolderRows", "saveFolders"];
const page = new Function("state", "el", "t", "escapeHtml", "postJson", "renderSettingsCategory",
                          "refreshThemePreview",
  NAMES.map(function (name) { return fn("function " + name + "("); }).join("\n") +
  "\nreturn {" + NAMES.map(function (n) { return n + ": " + n; }).join(", ") + "};");

function view() {
  const p = page(state, el, t, escapeHtml, postJson, renderSettingsCategory, refreshThemePreview);
  return p;
}
function look(p) {
  p.refreshServedDraftNote();
  return { unsaved: p.servedDraftUnsaved(), note: note.hidden ? "" : note.textContent,
           bar: el.settingsSavebarText.textContent,
           barDirty: el.settingsSavebarText.classList.has("is-dirty"),
           saveDisabled: el.settingsSaveBtn.disabled };
}

const TWENTY = [];
for (let n = 1; n <= 20; n++) { TWENTY.push({ name: "Shelf " + n, path: "X:\\Shelf " + n }); }
function copy(list) { return JSON.parse(JSON.stringify(list)); }
const out = {};

// The operator's case: twenty saved, every row removed, nothing else edited.
state = { folders: copy(TWENTY), foldersDraft: [], foldersSource: "file", listsSource: "",
          settingsDirty: {}, settingsActiveCategory: "your-list" };
let p = view();
out.removedAll = look(p);
out.removedAllSummary = p.foldersSectionHtml();

// Nothing edited.
state.foldersDraft = copy(TWENTY);
out.untouched = look(p);

// A blank row added and not filled in is not an edit.
state.foldersDraft.push({ name: "", path: "  " });
out.blankRow = look(p);

// One saved folder, then a settings edit as well as the folder edit.
state.folders = [TWENTY[0]];
state.foldersDraft = [];
state.settingsDirty = { LIST_BASE_NAME: "Shelves" };
out.oneSavedAndASetting = look(p);
state.settingsDirty = {};

// No folder list saved at all: the Music directory is served, and the
// server reports it as the one folder served (#1357).
state.folders = [{ name: "Music", path: "X:\\Music" }];
state.foldersSource = "file_directory";
state.foldersDraft = [{ name: "", path: "X:\\New Shelf" }];
out.directory = look(p);
out.directorySummary = p.foldersSectionHtml();

// Typing reaches the note without a re-render.
state = { folders: copy(TWENTY.slice(0, 2)), foldersDraft: copy(TWENTY.slice(0, 2)),
          foldersSource: "file", listsSource: "", settingsDirty: {}, settingsActiveCategory: "your-list" };
p = view();
rows = [rowFor(0), rowFor(1)];
p.attachFolderRows();
out.beforeTyping = look(p).unsaved;
rows[1].path.type("X:\\Elsewhere");
out.afterTyping = { unsaved: p.servedDraftUnsaved(), note: note.hidden ? "" : note.textContent,
                    bar: el.settingsSavebarText.textContent };

// A save writes it, and the editor is saved again.
answer = { ok: true, status: 200, data: { folders: [TWENTY[0], { name: "Elsewhere", path: "X:\\Elsewhere" }],
                                          source: "file", written: 2 } };
p.saveFolders();
out.afterSave = look(p);

// The lists editor: seeded from what is saved, exactly as loadLists() does.
const SAVED_LISTS = [{ name: "Main", primary: true, channels: ["#Shelf"], modes: { "#shelf": "normal" },
                       exclude: [".cue", ".nfo"], folders: copy(TWENTY.slice(0, 3)) },
                     { name: "Films", primary: false, channels: [], modes: {}, exclude: [], folders: [] }];
function seeded() {
  return SAVED_LISTS.map(function (entry) {
    return { name: entry.name, primary: !!entry.primary, channels: entry.channels.slice(),
             modes: Object.assign({}, entry.modes), exclude: entry.exclude.join(", "),
             folders: entry.folders.map(function (f) { return { name: f.name, path: f.path }; }) };
  });
}
state = { lists: copy(SAVED_LISTS), listsDraft: seeded(), listsSource: "file", folders: [], foldersDraft: [],
          settingsDirty: {}, settingsActiveCategory: "your-list" };
p = view();
out.listsSeeded = look(p);
state.listsDraft[0].folders.splice(0, 1);
out.listsFolderRemoved = look(p);
state.listsDraft = seeded();
state.listsDraft[1].exclude = ".sfv";
out.listsExcludeTyped = look(p);
state.listsDraft = seeded();
state.lists[0].modes = {};
out.listsNormalMode = look(p);
state.listsDraft = null;
out.listsNoDraft = look(p);

console.log(JSON.stringify(out));
"""


_RESULT = []


def run_the_page():
    """The harness's output, run once for both classes below."""
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
class TheFolderEditor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_the_page()

    def test_removing_every_row_is_not_saved_and_the_page_says_so(self):
        got = self.result["removedAll"]
        self.assertEqual(got["unsaved"], "folders")
        self.assertEqual(got["note"], "settings.foldersNotSavedMany 20")
        self.assertEqual(got["bar"], "settings.savebarFoldersNotSaved")
        self.assertTrue(got["barDirty"])
        # The page's Save saves the editor too (#1357), so it is offered.
        self.assertFalse(got["saveDisabled"])

    def test_the_summary_counts_what_is_saved_not_the_rows_on_screen(self):
        html = self.result["removedAllSummary"]
        self.assertIn('<p class="served-folder-summary">settings.servingManyFolders 20</p>', html)
        self.assertIn("served-draft-unsaved", html)

    def test_an_untouched_editor_is_saved(self):
        got = self.result["untouched"]
        self.assertEqual((got["unsaved"], got["note"], got["bar"], got["barDirty"]),
                         ("", "", "settings.allChangesSaved", False))

    def test_a_blank_row_is_not_an_edit(self):
        self.assertEqual(self.result["blankRow"]["unsaved"], "")

    def test_one_saved_folder_and_a_settings_edit_are_both_named(self):
        got = self.result["oneSavedAndASetting"]
        self.assertEqual(got["note"], "settings.foldersNotSavedOne 1")
        self.assertEqual(got["bar"], "settings.oneUnsavedChange 1 \u00b7 settings.savebarFoldersNotSaved")
        self.assertFalse(got["saveDisabled"])

    def test_with_no_folder_list_saved_the_music_directory_is_the_one_folder_served(self):
        """#1357: the Music directory is the editor's first row, so the page
        counts it like any saved folder."""
        self.assertEqual(self.result["directory"]["note"], "settings.foldersNotSavedOne 1")
        self.assertIn('<p class="served-folder-summary">settings.servingOneFolder 1</p>',
                      self.result["directorySummary"])

    def test_typing_updates_the_note_without_a_redraw(self):
        self.assertEqual(self.result["beforeTyping"], "")
        got = self.result["afterTyping"]
        self.assertEqual(got["unsaved"], "folders")
        self.assertEqual(got["note"], "settings.foldersNotSavedMany 2")
        self.assertEqual(got["bar"], "settings.savebarFoldersNotSaved")

    def test_a_save_makes_it_saved(self):
        got = self.result["afterSave"]
        self.assertEqual((got["unsaved"], got["note"], got["bar"]), ("", "", "settings.allChangesSaved"))


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class TheListEditor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_the_page()

    def test_lists_seeded_from_what_is_saved_are_saved(self):
        """The exclusions are text in the editor and a list on the server,
        and a channel's "normal" mode is the same as none."""
        got = self.result["listsSeeded"]
        self.assertEqual((got["unsaved"], got["bar"]), ("", "settings.allChangesSaved"))

    def test_a_folder_removed_from_a_list_is_not_saved_until_save_lists(self):
        got = self.result["listsFolderRemoved"]
        self.assertEqual(got["unsaved"], "lists")
        self.assertEqual(got["note"], "settings.listsNotSaved")
        self.assertEqual(got["bar"], "settings.savebarListsNotSaved")

    def test_typed_exclusions_are_an_edit(self):
        self.assertEqual(self.result["listsExcludeTyped"]["unsaved"], "lists")

    def test_no_draft_is_nothing_unsaved(self):
        self.assertEqual(self.result["listsNoDraft"]["unsaved"], "")

    def test_a_normal_mode_is_the_same_as_none(self):
        """The editor records "normal" for a ticked channel; the server may
        store nothing for it."""
        self.assertEqual(self.result["listsNormalMode"]["unsaved"], "")


class EveryRenderShowsIt(unittest.TestCase):
    """The note starts hidden in the markup; the render of Your list fills it,
    so an editor left unsaved on another category still says so on return."""

    def test_the_render_refreshes_the_note_after_filling_the_rows(self):
        with open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            code = handle.read()
        self.assertEqual(code.count(
            '      if (state.listsSource === "file") { attachListRows(); } else { attachFolderRows(); }\n'
            '      refreshServedDraftNote();\n'), 1)


if __name__ == "__main__":
    unittest.main()
