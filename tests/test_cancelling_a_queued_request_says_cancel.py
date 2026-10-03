"""Cancelling a request the other bot has queued says "Cancel" (#977).

A fetch the other bot has queued has no file yet, and it got the Downloads
panel's remove button - labelled "Delete", and asking "Delete this fetched
file? This cannot be undone." The "Cancel" wording and the "Nothing has been
downloaded yet" prompt were given only to a row still pending here.

The behaviour test runs the real fetchRowNotStarted() under node, and is
skipped where node is not installed; the source guards run everywhere.
"""

import io
import os
import shutil
import subprocess
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


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class WhichRowsHaveNotStarted(unittest.TestCase):
    def test_pending_and_queued_have_not_started_and_the_rest_have(self):
        helper = body_of(app_js(), "function fetchRowNotStarted(")
        script = (helper + "\nconsole.log(['pending','queued','offered','receiving','complete','failed']"
                  ".map(function (s) { return s + '=' + fetchRowNotStarted(s); }).join(','));")
        done = subprocess.run(["node", "-e", script], capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stdout.decode("utf-8").strip(),
                         "pending=true,queued=true,offered=true,receiving=false,complete=false,failed=false")


class TheButtonAsksIt(unittest.TestCase):
    def test_the_label_and_the_prompt_both_follow_it(self):
        js = app_js()
        self.assertIn("var notStarted = fetchRowNotStarted(state);", js)
        self.assertIn('data-pending=\\"" + (notStarted ? "1" : "") + "\\">', js)
        self.assertIn('(notStarted ? t("common.cancel") : t("common.delete"))', js)


if __name__ == "__main__":
    unittest.main()
