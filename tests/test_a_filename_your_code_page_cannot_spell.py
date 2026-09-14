"""A path DCCore cannot print must not be a path DCCore cannot list.

Reported from a live install, on a Greek Windows box:

    External update_list.py failed (Exit Code 1): Unknown script error
    ...
    File "C:\\Python314\\Lib\\encodings\\cp1253.py", line 23, in decode
    UnicodeDecodeError: 'charmap' codec can't decode byte 0x8d in position 563

TWO FAILURES, ONE CAUSE, AND THEY HID EACH OTHER.

The CHILD: update_list.py runs as its own process, so oserve.py's console guard
- which has protected the daemon since it was written - did nothing for it.
Every line it prints is a path off somebody's disk, and on a console whose code
page cannot represent a character in one of those paths, print() raises
UnicodeEncodeError and the scan dies where it stood. A music library with one
accented filename is enough, which is to say: most of them.

The PARENT: subprocess.run(..., text=True) with no encoding decodes the child
with the same locale code page. So the bytes that did escape killed
subprocess's own reader thread, and the run reported "Unknown script error" -
because the output that would have explained it is exactly what could not be
read.

The operator got a failure with no cause, for a library that was fine.

THE FIX IS THE ONE THAT ALREADY EXISTED, applied where it was missing.
platform_compat.install_console_encoding_guard() has been the answer since
oserve.py called it; update_list.py, configure.py and adminchat.py are entry
points too and never did. The class guard below is the part that keeps this
fixed: a new entry point that forgets is a failing test, not a bug report from
somebody's channel.

And the parents now decode utf-8 with errors="replace", so a child that does
not guard itself still cannot take the daemon's report away with it. rar is
not Python and cannot be guarded at all - there, this is the whole fix.
"""

import ast
import io
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import platform_compat  # noqa: E402

# A code page that cannot spell any of these. Chosen because it is the one the
# report came from, and because it is nobody's default here - the assertions
# force it rather than depending on the machine running them.
NARROW_CODE_PAGE = "cp1253"
UNSPELLABLE = "Bjo\u0308rk \u2013 Jo\u0301ga \u00e9\u00e7"


def source(name):
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def entry_points():
    """Every module that can be started as a script of its own."""
    found = []
    for name in sorted(os.listdir(REPO_ROOT)):
        if not name.endswith(".py"):
            continue
        if '__name__ == "__main__"' in source(name):
            found.append(name)
    return found


class EveryEntryPointGuardsItsConsole(unittest.TestCase):
    """The class fix. oserve.py had this right; three others did not, and the
    one that mattered was the one that prints a path per file."""

    def test_there_are_entry_points_to_check(self):
        """Guard on the guard - an empty list would make the sweep below pass
        on nothing at all."""
        self.assertTrue(entry_points())

    def test_each_one_installs_the_guard(self):
        for name in entry_points():
            with self.subTest(module=name):
                self.assertIn("install_console_encoding_guard()", source(name),
                              "%s can be run as a script and prints operator "
                              "data, but never protects its console - a "
                              "filename outside the code page kills it" % name)

    def test_the_scanner_installs_it_before_it_can_print_a_path(self):
        """Order matters here and nowhere else: the guard is worthless if the
        first path is printed above it."""
        text = source("update_list.py")
        installed = text.index("install_console_encoding_guard()")
        walked = text.index("os.walk(")

        self.assertLess(installed, walked)


def setup_check_source():
    with io.open(os.path.join(REPO_ROOT, "scripts", "setup_check.py"),
                 encoding="utf-8") as handle:
        return handle.read()


class TheSetupCheckGuardsItsConsoleToo(unittest.TestCase):
    """#428. scripts/setup_check.py is not swept by entry_points() above - it
    has no `__name__ == "__main__"` of its own, since it is a library the two
    platform shims (scripts/windows|linux/check-setup.py) import and call
    main() on - but start-dccore.bat runs that shim with stdout redirected
    (`>nul 2>&1`), which loses PEP 528's console exemption exactly the way a
    real entry point's redirection would. A path in a configured setting
    that the machine's ANSI code page cannot spell then raised
    UnicodeEncodeError on one of the many prints in main(), which
    `import platform_compat` at the top of the file - never followed by the
    call - did nothing to stop.

    Kept as its own targeted class rather than folded into entry_points()
    above: this file has no __main__, so it is not naturally in that sweep's
    domain, and the two shims that DO have one are correctly covered
    TRANSITIVELY by main() installing the guard before its own first print -
    demanding the literal call in the shims' own source too would fail a
    correct design instead of catching a wrong one.
    """

    def test_main_installs_the_guard(self):
        self.assertIn("install_console_encoding_guard()", setup_check_source())

    def test_before_its_own_first_print(self):
        """Order matters here and nowhere else, same as update_list.py's own
        check above: the guard is worthless if a print already ran above it."""
        text = setup_check_source()
        installed = text.index("install_console_encoding_guard()")
        first_print = text.index("    print(")

        self.assertLess(installed, first_print,
                        "a print() call above the guard can still raise "
                        "UnicodeEncodeError on a narrow console code page")

    def test_the_shims_reach_it_by_calling_main(self):
        """The transitive half of the claim above, checked rather than
        assumed: both platform launchers must actually call setup_check.main()
        - the only path through which they inherit the guard - not reimplement
        the report themselves."""
        for shim in ("windows", "linux"):
            with io.open(os.path.join(REPO_ROOT, "scripts", shim, "check-setup.py"),
                        encoding="utf-8") as handle:
                text = handle.read()
            with self.subTest(shim=shim):
                self.assertIn("setup_check.main(", text)


class TheGuardSurvivesAPathItCannotSpell(unittest.TestCase):
    """Run for real in a child process with the code page forced, because
    this failure only exists at the boundary between a process and its
    console."""

    def run_child(self, code):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = NARROW_CODE_PAGE
        env.pop("PYTHONUTF8", None)
        return subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, cwd=REPO_ROOT,
            timeout=60)

    def test_without_the_guard_the_process_dies(self):
        """The bug, reproduced. If this ever stops failing, the assertion
        below is passing for a reason that has nothing to do with the fix."""
        done = self.run_child("print(%r)" % UNSPELLABLE)

        self.assertNotEqual(done.returncode, 0)
        self.assertIn("UnicodeEncodeError", done.stderr)

    def test_with_the_guard_it_prints(self):
        done = self.run_child(
            "import platform_compat;"
            "platform_compat.install_console_encoding_guard();"
            "print(%r)" % UNSPELLABLE)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.strip())

    def test_and_what_it_prints_is_still_recognisable(self):
        """errors="replace", not errors="ignore": a name reduced to nothing is
        a log line that cannot be matched to a file."""
        done = self.run_child(
            "import platform_compat;"
            "platform_compat.install_console_encoding_guard();"
            "print(%r)" % UNSPELLABLE)

        self.assertIn("rk", done.stdout)
        self.assertIn("ga", done.stdout)


class NoParentDecodesWithTheLocaleCodePage(unittest.TestCase):
    """text=True on its own means "decode with whatever the console uses",
    which is the parent half of the same bug."""

    def captured_runs(self, name):
        """Every subprocess call in `name` that captures output, as AST."""
        tree = ast.parse(source(name))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "run"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"):
                continue
            kwargs = {kw.arg for kw in node.keywords}
            if "capture_output" in kwargs or "stdout" in kwargs:
                found.append((node, kwargs))
        return found

    def test_there_are_calls_to_check(self):
        every = []
        for name in ("commands.py", "dcc.py", "update_list.py"):
            every.extend(self.captured_runs(name))

        self.assertTrue(every, "no captured subprocess runs found - has the "
                               "pattern gone stale?")

    def test_each_one_names_its_encoding(self):
        for name in ("commands.py", "dcc.py", "update_list.py"):
            for node, kwargs in self.captured_runs(name):
                with self.subTest(module=name, line=node.lineno):
                    self.assertIn(
                        "encoding", kwargs,
                        "%s:%d captures output without naming an encoding, so "
                        "it decodes with the console code page and one byte "
                        "outside it kills the reader thread"
                        % (name, node.lineno))

    def test_and_replaces_rather_than_raising(self):
        for name in ("commands.py", "dcc.py", "update_list.py"):
            for node, kwargs in self.captured_runs(name):
                with self.subTest(module=name, line=node.lineno):
                    self.assertIn(
                        "errors", kwargs,
                        "%s:%d would still raise on a byte utf-8 cannot "
                        "decode - rar's output is not ours to guarantee"
                        % (name, node.lineno))

    def test_the_values_are_the_ones_intended(self):
        """The keyword being present is not the same as it being right."""
        for name in ("commands.py", "dcc.py", "update_list.py"):
            text = source(name)
            with self.subTest(module=name):
                self.assertIn('encoding="utf-8"', text)
                self.assertIn('errors="replace"', text)


class TheGuardItselfStillDoesWhatItSays(unittest.TestCase):
    """It is now load-bearing for four processes rather than one."""

    def test_a_stream_with_no_reconfigure_is_not_an_error(self):
        class Bare:
            encoding = "cp1253"

        self.assertEqual(
            platform_compat.install_console_encoding_guard([("out", Bare())]),
            [])

    def test_none_is_not_an_error(self):
        """pythonw.exe gives None for both."""
        self.assertEqual(
            platform_compat.install_console_encoding_guard([("out", None)]),
            [])

    def test_it_reports_what_it_changed(self):
        class Fake:
            encoding = "cp1253"

            def __init__(self):
                self.calls = []

            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)

        stream = Fake()
        changed = platform_compat.install_console_encoding_guard(
            [("out", stream)])

        self.assertEqual(changed, ["out"])
        self.assertEqual(stream.calls[0]["encoding"], "utf-8")
        self.assertEqual(stream.calls[0]["errors"], "replace")


if __name__ == "__main__":
    unittest.main()
