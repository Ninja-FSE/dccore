"""The real oserve module was imported by every test process (via list.py)
and wrapped the runner's stdout and stderr with the timestamp proxy
(audit L43, #707).

list.py imports oserve, announce imports list, and tests/support.py
imports announce - so oserve.py's two module-level installs (the console
encoding guard and the _TimestampedStream proxy) ran in every test process
and wrapped sys.stdout and sys.stderr for the rest of the run: unittest's
summaries came out timestamped, and a test asserting an exact printed line
saw a prefix that depended on which module was imported first.
install_fake_oserve()'s docstring said the real import was avoided and
would start worker threads; neither was true.

The installs sit under `if __name__ == "__main__":` at the top of oserve.py
now - true exactly when `python oserve.py` is the program, so the daemon's
first lines are still stamped and guarded - and an import touches nothing.
Checked in child processes, where the streams start clean.
"""

import os
import re
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

STAMP = re.compile(r"^\[\d\d:\d\d:\d\d\] ")


def child(code):
    # PYTHONPATH, not cwd alone (#959): most daemon modules live in src/ now,
    # and a bare "import announce" (unlike "import oserve", which fixes up
    # sys.path for the rest of the process once IT is imported) needs to find
    # it without going through oserve.py first.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env["PYTHONPATH"] = os.path.join(REPO_ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120,
                          env=env)
    return done


class AnImport(unittest.TestCase):

    def test_importing_announce_leaves_the_streams_alone(self):
        """The audit's probe: `import announce; print('x')` printed
        '[22:35:29] x'."""
        done = child("import sys, announce; print(type(sys.stdout).__name__); print('x')")

        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        self.assertNotIn("_TimestampedStream", lines[0])
        self.assertEqual(lines[-1], "x")

    def test_importing_oserve_itself_does_too_and_starts_nothing(self):
        done = child("import sys, threading, oserve; "
                     "print(type(sys.stdout).__name__, type(sys.stderr).__name__, threading.active_count(), 'oserve' in sys.modules)")

        self.assertEqual(done.returncode, 0, done.stderr)
        kind_out, kind_err, threads, present = done.stdout.split()
        self.assertNotEqual(kind_out, "_TimestampedStream")
        self.assertNotEqual(kind_err, "_TimestampedStream")
        self.assertEqual(threads, "1", "importing oserve started a thread")
        self.assertEqual(present, "True")


class TheProgram(unittest.TestCase):

    def test_run_as_a_script_the_first_line_is_stamped(self):
        """What the guard must not cost: `python oserve.py` still stamps and
        guards from its first line. Driven with startup() and run_forever()
        replaced, so no daemon starts and no network is touched: the file
        is executed as __main__ up to its entry point."""
        import io
        import tempfile
        with io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            src = handle.read()
        entry_start = src.index(chr(10) + "    startup()" + chr(10), src.index("def run_forever(")) + 1
        entry_end = src.index("    run_forever()", entry_start) + len("    run_forever()")
        entry = src[entry_start:entry_end]
        self.assertIn("stopping.ensure_watcher()", entry, "the stop watcher (#1065) starts at the entry")
        stub = src.replace(entry, '    print("reached the entry point")')
        # Run from the repository, so the console log (#1065) would be the
        # checkout's own data/logs/dccore.log: not this test's to write.
        log_install = "    platform_compat.install_console_log(_console_log_settings)"
        self.assertIn(log_install, stub)
        stub = stub.replace(log_install, "    pass")
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8",
                                         dir=REPO_ROOT, prefix="_oserve_as_main_") as handle:
            handle.write(stub)
        self.addCleanup(os.remove, handle.name)
        done = subprocess.run([sys.executable, handle.name], cwd=REPO_ROOT, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120,
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"))

        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        reached = [l for l in done.stdout.splitlines() if "reached the entry point" in l]
        self.assertEqual(len(reached), 1, done.stdout[-500:])
        self.assertRegex(reached[0], STAMP)


class TheStubsDocstringIsTrue(unittest.TestCase):

    def test_it_no_longer_claims_the_import_is_avoided(self):
        from tests import support
        doc = support.install_fake_oserve.__doc__

        self.assertIn("it IS imported here", doc)
        self.assertNotIn("would start worker", doc)


if __name__ == "__main__":
    unittest.main()
