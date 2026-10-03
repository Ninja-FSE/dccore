"""What is typed in the on-connect box survives the page redrawing it (#1090).

Settings > Identity & network rebuilds its panel for every note it shows. The
on-connect box was then filled from the SAVED commands, so pressing Resend with
an edited box - which says "save the commands first" - threw the edited lines
away beside that advice, and a refused Save did the same. The box keeps a draft
now, until a save succeeds.
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

const SAVED = ["PRIVMSG X :LOGIN someaccount old-word", "MODE %nick% +x"];
const state = { onConnect: { commands: SAVED.slice(), delay_seconds: 2 },
                onConnectNote: null, onConnectDraft: null };
let page = null;
const el = { settingsFields: { querySelector: function (selector) {
  return selector === ".on-connect-commands" ? page.box : page.delay;
} } };

// Every render is a new panel: new elements, filled by attachOnConnectRows().
let attach = null;
function render() { page = { box: fakeInput(), delay: fakeInput() }; attach(); }

let answer = null;
const posted = [];
function postJson(url, body) {
  posted.push(url);
  return { then: function (callback) { callback(answer); } };
}
function t(key) { return key; }

const make = function (signature, name) {
  return new Function("state", "el", "postJson", "renderSettingsCategory", "t",
    fn(signature) + "\nreturn " + name + ";")(state, el, postJson, render, t);
};
attach = make("function attachOnConnectRows(", "attachOnConnectRows");
const resend = make("function resendOnConnect(", "resendOnConnect");
const save = make("function saveOnConnect(", "saveOnConnect");

const out = {};
render();
out.first = page.box.value;

const EDITED = "PRIVMSG X :LOGIN someaccount new-word\nMODE %nick% +x";
// The box alone first: each field keeps the draft by itself.
page.box.type(EDITED);
resend();
out.afterResend = { box: page.box.value, delay: page.delay.value, note: state.onConnectNote.text,
                    posted: posted.slice() };

page.delay.type("4");
answer = { ok: false, status: 400, data: { error: "line 1 is too long" } };
save();
out.afterRefusedSave = { box: page.box.value, delay: page.delay.value, ok: state.onConnectNote.ok };

answer = { ok: true, status: 200,
           data: { commands: EDITED.split("\n"), delay_seconds: 4, message: "Saved." } };
save();
out.afterSave = { box: page.box.value, delay: page.delay.value, draft: state.onConnectDraft };

// Saved now: what the server wrote is what the next render shows.
state.onConnect.commands = ["MODE %nick% +x"];
render();
out.serverWins = page.box.value;
console.log(JSON.stringify(out));
"""

EDITED = "PRIVMSG X :LOGIN someaccount new-word\nMODE %nick% +x"


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class TheBox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
        cls.result = json.loads(done.stdout.decode("utf-8"))

    def test_it_starts_with_the_saved_commands(self):
        self.assertEqual(self.result["first"], "PRIVMSG X :LOGIN someaccount old-word\nMODE %nick% +x")

    def test_resend_says_save_first_and_keeps_the_typing(self):
        got = self.result["afterResend"]
        self.assertEqual(got["note"], "settings.resendSaveFirst")
        self.assertEqual(got["posted"], [])
        self.assertEqual((got["box"], str(got["delay"])), (EDITED, "2"))

    def test_a_refused_save_keeps_it_too(self):
        got = self.result["afterRefusedSave"]
        self.assertFalse(got["ok"])
        self.assertEqual((got["box"], got["delay"]), (EDITED, "4"))

    def test_a_save_lets_the_draft_go(self):
        got = self.result["afterSave"]
        self.assertIsNone(got["draft"])
        self.assertEqual(got["box"], EDITED)
        self.assertEqual(self.result["serverWins"], "MODE %nick% +x")


if __name__ == "__main__":
    unittest.main()
