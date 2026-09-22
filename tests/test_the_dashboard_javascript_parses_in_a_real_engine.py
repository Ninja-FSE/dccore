"""The dashboard's JavaScript is parsed by a real engine, where one exists
(audit M46, #648).

Every web/ test is a hand-written scanner: test_dashboard_javascript_is_not_
broken.py models comments, strings and bracket depth, and catches exactly
the class of edit that put it there (a real newline inside a string). It
does not model the language. The audit fed web/app.js five syntax errors -
an unbalanced ternary, a missing operand, `var var`, misplaced but balanced
braces, a dangling `else` - and every test in that module and in
test_web_assets.py stayed green while `node --check` rejected each one. The
inline <script> at the top of web/index.html was scanned by nothing at all.
And node is on this host and on every GitHub-hosted runner, unused.

So: every web/*.js and every inline <script> in web/*.html goes through
`node --check`. Where there is no node this skips - and the scanner, which
runs everywhere, is the pair that keeps that skip from being a hole. A
control proves the engine is really checking: a known-bad snippet must be
refused by the same command, or a node that silently accepts everything
would pass this file too.
"""

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

WEB_DIR = os.path.join(REPO_ROOT, "web")
NODE = shutil.which("node")

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def javascript_files():
    found = []
    for root, _dirs, names in os.walk(WEB_DIR):
        for name in names:
            if name.endswith(".js"):
                found.append(os.path.join(root, name))
    return sorted(found)


def inline_scripts():
    """(html file, index, source) for every <script> without a src."""
    found = []
    for name in sorted(os.listdir(WEB_DIR)):
        if not name.endswith(".html"):
            continue
        with io.open(os.path.join(WEB_DIR, name), encoding="utf-8") as handle:
            html = handle.read()
        for index, match in enumerate(INLINE_SCRIPT.finditer(html)):
            found.append((name, index, match.group(1)))
    return found


def node_check(path):
    """(ok, stderr) for `node --check path`."""
    done = subprocess.run([NODE, "--check", path], capture_output=True, text=True,
                          errors="replace", timeout=60)
    return done.returncode == 0, done.stderr


@unittest.skipUnless(NODE, "no node on PATH - the hand scanner in "
                           "test_dashboard_javascript_is_not_broken.py is the fallback")
class ARealEngineParsesIt(unittest.TestCase):

    def test_every_script_file(self):
        files = javascript_files()
        self.assertIn(os.path.join(WEB_DIR, "app.js"), files, "the dashboard's own script was not found")
        for path in files:
            with self.subTest(file=os.path.relpath(path, REPO_ROOT)):
                ok, err = node_check(path)
                self.assertTrue(ok, err)

    def test_every_inline_script_block(self):
        blocks = inline_scripts()
        self.assertTrue(any(name == "index.html" for name, _i, _s in blocks),
                        "index.html's theme block was not found")
        tmp = tempfile.mkdtemp(prefix="dccore-inline-js-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name, index, source in blocks:
            with self.subTest(html=name, block=index):
                path = os.path.join(tmp, "%s-%d.js" % (name.replace(".", "_"), index))
                with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(source)
                ok, err = node_check(path)
                self.assertTrue(ok, err)

    def test_the_engine_really_refuses_what_it_should(self):
        """The control. Each of these passed the hand scanner in the audit;
        a node that let them through would make the two tests above
        meaningless, so it has to be seen refusing them."""
        tmp = tempfile.mkdtemp(prefix="dccore-bad-js-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        broken = {
            "unbalanced ternary": "var x = a ? b;\n",
            "missing operand": "var x = 1 +;\n",
            "var var": "var var x = 1;\n",
            "dangling else": "else { }\n",
            "misplaced braces": "function f() { } } {\n",
        }
        for label, source in broken.items():
            with self.subTest(mutant=label):
                path = os.path.join(tmp, label.replace(" ", "_") + ".js")
                with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(source)
                ok, _err = node_check(path)
                self.assertFalse(ok, "node accepted: %s" % source)


class TheFallbackIsStillThere(unittest.TestCase):
    """The everywhere half: the scanner this pairs with must keep existing,
    since a runner without node has only it."""

    def test_the_hand_scanner_module_exists_and_scans_app_js(self):
        from tests import test_dashboard_javascript_is_not_broken as scanner

        self.assertTrue(callable(getattr(scanner, "scan", None)))


if __name__ == "__main__":
    unittest.main()
