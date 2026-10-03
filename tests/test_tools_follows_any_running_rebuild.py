"""The Tools page follows a list rebuild however it was started (#1023).

Its progress bar used to follow only a rebuild started by its own button. One
started by the console's `update` (the @DCCore window in mIRC), by !update in
IRC or by LIST_REBUILD_SCHEDULE ran unseen, and pressing the button meanwhile
answered only "A list update is already running." Now the page takes up
whatever is running: when Tools is opened, while it is open, and when the
button is refused.

The behaviour test runs the real followRunningUpdate() and
loadUpdateListSchedule() under node, and is skipped where node is not
installed; the source guards run everywhere.
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


def body_of(code, signature):
    start = code.index(signature)
    depth = 0
    for at in range(code.index("{", start), len(code)):
        if code[at] == "{":
            depth += 1
        elif code[at] == "}":
            depth -= 1
            if depth == 0:
                return code[start:at + 1]
    raise AssertionError("unbalanced braces after " + signature)


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
const code = ["function followRunningUpdate(", "function loadUpdateListSchedule("].map(fn).join("\n");

function run(payload, alreadyPolling) {
  const started = [];
  const button = { disabled: false };
  const env = {
    el: { updateListSchedule: { textContent: "" }, updateListRunBtn: button },
    updateList: { pollTimer: alreadyPolling ? 1 : null },
    fetchJson: function () { return Promise.resolve(payload); },
    t: function (key) { return key; },
    startUpdateListPolling: function () { started.push(1); },
  };
  const page = new Function("el", "updateList", "fetchJson", "t", "startUpdateListPolling",
    code + "\nreturn { load: loadUpdateListSchedule, follow: followRunningUpdate };")(
    env.el, env.updateList, env.fetchJson, env.t, env.startUpdateListPolling);
  page.load();
  return new Promise(function (resolve) {
    setTimeout(function () { resolve(started.length + ":" + button.disabled); }, 0);
  });
}

Promise.all([
  run({ running: true, progress: {} }, false),
  run({ running: false, schedule: "" }, false),
  run({ running: true, progress: {} }, true),
]).then(function (seen) {
  console.log(["running=" + seen[0], "idle=" + seen[1], "followed=" + seen[2]].join("\n"));
});
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class OpeningToolsTakesItUp(unittest.TestCase):

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

    def test_a_running_rebuild_is_followed_and_the_button_waits(self):
        self.assertEqual(self.seen()["running"], "1:true")

    def test_nothing_running_starts_nothing(self):
        self.assertEqual(self.seen()["idle"], "0:false")

    def test_one_already_followed_is_not_followed_twice(self):
        self.assertEqual(self.seen()["followed"], "0:false")


class TheOtherWaysIn(unittest.TestCase):
    """What the node test checks, and the two triggers it cannot reach."""

    def test_a_refused_button_asks_status_and_follows(self):
        js = app_js()
        handler = js[js.index('el.updateListRunBtn.addEventListener("click"'):]
        handler = handler[:handler.index("function showUpdateListStatus(")]
        refused = handler[handler.index("if (!res.ok) {"):handler.index("startUpdateListPolling();")]
        self.assertIn('fetchJson("/api/tools/update-list/status")', refused)
        self.assertIn("if (followRunningUpdate(payload)) { return; }", refused)

    def test_the_tick_picks_one_up_while_tools_is_open(self):
        self.assertIn('if (state.active === "tools" && !updateList.pollTimer) { loadUpdateListSchedule(); }',
                      app_js())

    def test_opening_tools_asks(self):
        self.assertIn('if (name === "tools") { loadUpdateListSchedule(); }', app_js())

    def test_loading_the_schedule_follows_what_is_running(self):
        body = body_of(app_js(), "function loadUpdateListSchedule(")
        self.assertIn("followRunningUpdate(payload);", body)


if __name__ == "__main__":
    unittest.main()
