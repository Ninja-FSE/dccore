"""The suite ran in one process: five to ten minutes, several times a preflight (#1146).

scripts/run_tests_in_parallel.py splits the test modules into a few shards,
balanced by tests/module_durations.json, runs each in its own
`python -m unittest` process and reports the lot as ONE unittest run. CI runs
it, and preflight's plain and counting passes do. These tests hold it to the
promises the rest of the tooling relies on:

* every module runs exactly once, so the total is the serial run's total;
* a shard keeps the serial run's order, alphabetical, with modules only
  taken out;
* the merged report has one "Ran N tests" line and one verdict, which is
  what CI's log and preflight's count and skip parsing read;
* a shard that crashes, or exits non-zero after printing OK, fails the run.
"""

import importlib.util
import io
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.support import parse_source, temp_dir  # noqa: E402


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_tests_in_parallel_under_test", os.path.join(REPO_ROOT, "scripts", "run_tests_in_parallel.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = load_runner()


def unittest_output(ran, verdict="OK", counts="", progress="", failures="", trailing=""):
    """What `python -m unittest` prints, in the shape it prints it."""
    summary = verdict + (f" ({counts})" if counts else "")
    return (progress + "\n" + failures + "-" * 70 + "\n"
            + f"Ran {ran} test{'' if ran == 1 else 's'} in 1.234s\n\n" + summary + "\n" + trailing)


FAILURE_BLOCK = ("=" * 70 + "\nFAIL: test_x (tests.test_b.Case.test_x)\n" + "-" * 70 + "\n"
                 "Traceback (most recent call last):\nAssertionError: 1 != 2\n\n")


def merged(shards, finished):
    out = io.StringIO()
    ok = RUNNER.report(shards, finished, 12.5, out)
    return ok, out.getvalue()


class TheShards(unittest.TestCase):

    def test_every_module_lands_in_exactly_one_shard_in_alphabetical_order(self):
        modules = [f"tests.test_{n:03d}" for n in range(57)]
        durations = {m: float(n % 7 + 1) for n, m in enumerate(modules)}

        shards = RUNNER.make_shards(modules, durations, 4)

        self.assertEqual(len(shards), 4)
        self.assertEqual(sorted(m for shard in shards for m in shard), modules)
        for shard in shards:
            self.assertEqual(shard, sorted(shard))

    def test_the_longest_modules_are_spread_and_the_loads_are_close(self):
        durations = {"tests.test_slow_a": 30.0, "tests.test_slow_b": 29.0, "tests.test_slow_c": 28.0}
        durations.update({f"tests.test_quick_{n:02d}": 1.0 for n in range(60)})

        shards = RUNNER.make_shards(sorted(durations), durations, 3)

        loads = [sum(durations[m] for m in shard) for shard in shards]
        slow_per_shard = [sum(1 for m in shard if "slow" in m) for shard in shards]
        self.assertEqual(slow_per_shard, [1, 1, 1])
        self.assertLessEqual(max(loads) - min(loads), 1.0)

    def test_a_module_nobody_measured_counts_as_the_median(self):
        durations = {"tests.test_big": 20.0, "tests.test_a": 8.0, "tests.test_b": 8.0,
                     "tests.test_c": 1.0, "tests.test_d": 1.0}

        shards = RUNNER.make_shards(sorted(durations) + ["tests.test_new"], durations, 2)

        # Weighed as 8.0, the new module is placed before c and d, which then
        # top up the shard holding the big one. Weighed as nothing, it would
        # come last and the big one would be left alone.
        self.assertIn(["tests.test_big", "tests.test_c", "tests.test_d"], shards)
        self.assertIn(["tests.test_a", "tests.test_b", "tests.test_new"], shards)

    def test_more_shards_than_modules_leaves_some_empty(self):
        shards = RUNNER.make_shards(["tests.test_a"], {}, 4)

        self.assertEqual([s for s in shards if s], [["tests.test_a"]])

    def test_a_table_that_cannot_be_read_is_no_table(self):
        folder = temp_dir(self)
        self.assertEqual(RUNNER.load_durations(os.path.join(folder, "missing.json")), {})
        for n, content in enumerate(("not json", "[1, 2]", '{"tests.test_a": "fast", "tests.test_b": true}')):
            path = os.path.join(folder, f"table{n}.json")
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            self.assertEqual(RUNNER.load_durations(path), {}, content)

    def test_the_shipped_table_is_readable(self):
        table = RUNNER.load_durations()

        self.assertGreater(len(table), 100)
        self.assertTrue(all(name.startswith("tests.test") for name in table))


class EveryModuleRunsOnce(unittest.TestCase):

    def test_the_modules_hold_exactly_the_tests_discover_finds(self):
        """The total a parallel run reports is the serial run's total."""
        loader = unittest.TestLoader()
        discovered = loader.discover(os.path.join(REPO_ROOT, "tests"), top_level_dir=REPO_ROOT)
        named = loader.loadTestsFromNames(RUNNER.test_modules())

        self.assertEqual(named.countTestCases(), discovered.countTestCases())

    def test_and_each_is_named_once(self):
        modules = RUNNER.test_modules()

        self.assertEqual(len(modules), len(set(modules)))
        self.assertIn("tests." + os.path.splitext(os.path.basename(__file__))[0], modules)


class TheReportReadsAsOneRun(unittest.TestCase):

    def test_the_counts_are_summed_into_one_summary(self):
        shards = [["tests.test_a"], ["tests.test_b"]]
        finished = [(0, unittest_output(10, counts="skipped=2", progress="....ss...."), 3.0),
                    (0, unittest_output(5, progress="....."), 2.0)]

        ok, said = merged(shards, finished)

        self.assertTrue(ok)
        self.assertEqual(re.findall(r"Ran (\d+) tests", said), ["15"])
        self.assertTrue(said.rstrip().endswith("OK (skipped=2)"), said[-200:])

    def test_what_preflight_parses_reads_the_total(self):
        """preflight takes the FIRST "Ran N tests" and the first "skipped=N"."""
        shards = [["tests.test_a"], ["tests.test_b"]]
        finished = [(0, unittest_output(10, counts="skipped=2"), 3.0),
                    (0, unittest_output(7, counts="skipped=1"), 2.0)]

        _ok, said = merged(shards, finished)

        self.assertEqual(re.search(r"Ran (\d+) tests", said).group(1), "17")
        self.assertEqual(re.search(r"skipped=(\d+)", said).group(1), "3")

    def test_verbose_lines_and_skip_reasons_survive(self):
        shards = [["tests.test_a"], ["tests.test_b"]]
        finished = [(0, unittest_output(1, counts="skipped=1", progress=(
                        "test_one (tests.test_a.Case.test_one) ... skipped 'no POSIX shell on PATH'\n")), 1.0),
                    (0, unittest_output(1, progress="test_two (tests.test_b.Case.test_two) ... ok\n"), 1.0)]

        _ok, said = merged(shards, finished)

        self.assertIn("test_one (tests.test_a.Case.test_one) ... skipped 'no POSIX shell on PATH'\n", said)
        self.assertIn("test_two (tests.test_b.Case.test_two) ... ok\n", said)
        self.assertLess(said.index("test_two"), said.index("Ran 2 tests"))

    def test_a_failure_fails_the_run_and_its_traceback_comes_before_the_summary(self):
        shards = [["tests.test_a"], ["tests.test_b"]]
        finished = [(0, unittest_output(3), 1.0),
                    (1, unittest_output(4, verdict="FAILED", counts="failures=1, skipped=1",
                                        progress="..F.", failures=FAILURE_BLOCK), 1.0)]

        ok, said = merged(shards, finished)

        self.assertFalse(ok)
        self.assertLess(said.index("FAIL: test_x (tests.test_b.Case.test_x)"), said.index("Ran 7 tests"))
        self.assertTrue(said.rstrip().endswith("FAILED (failures=1, skipped=1)"), said[-200:])
        self.assertIn("run it again: python -m unittest tests.test_b\n", said)

    def test_a_shard_that_never_reached_its_summary_is_an_error(self):
        shards = [["tests.test_a"], ["tests.test_b"]]
        finished = [(0, unittest_output(3), 1.0),
                    (3, "....Fatal Python error: Segmentation fault\n", 1.0)]

        ok, said = merged(shards, finished)

        self.assertFalse(ok)
        self.assertIn("ERROR: shard 2 of 2 ended without a summary", said)
        self.assertIn("Fatal Python error", said)
        self.assertTrue(said.rstrip().endswith("FAILED (errors=1)"), said[-200:])

    def test_ok_with_a_failing_exit_status_is_not_a_pass(self):
        shards = [["tests.test_a"]]
        finished = [(1, unittest_output(3), 1.0)]

        ok, said = merged(shards, finished)

        self.assertFalse(ok)
        self.assertTrue(said.rstrip().endswith("FAILED (errors=1)"), said[-200:])

    def test_a_line_printed_after_the_summary_is_kept_and_does_not_hide_it(self):
        shards = [["tests.test_a"]]
        finished = [(0, unittest_output(3, trailing="[LATE THREAD] still talking\n"), 1.0)]

        ok, said = merged(shards, finished)

        self.assertTrue(ok)
        self.assertIn("[LATE THREAD] still talking", said)
        self.assertEqual(re.findall(r"Ran (\d+) tests", said), ["3"])

    def test_a_summary_a_test_printed_is_not_the_shards(self):
        """A test that runs an inner unittest and lets it print: the shard's
        own summary is the last one, not the first."""
        inner = "-" * 70 + "\nRan 2 tests in 0.001s\n\nOK\n"
        shards = [["tests.test_a"]]
        finished = [(0, unittest_output(40, progress="...." + "\n" + inner + "...."), 1.0)]

        _ok, said = merged(shards, finished)

        self.assertEqual(re.findall(r"Ran (\d+) tests", said), ["2", "40"])
        self.assertTrue(said.rstrip().endswith("\nOK"), said[-200:])

    def test_one_test_is_singular(self):
        ok, said = merged([["tests.test_a"]], [(0, unittest_output(1), 1.0)])

        self.assertIn("\nRan 1 test in ", said)


class ARealRun(unittest.TestCase):
    """Two real shards of two small real modules: the report is parsed from
    what this Python's unittest actually prints."""

    def test_two_small_modules_in_two_shards(self):
        modules = ["tests.test_the_suite_runs_in_balanced_shards_sample_a",
                   "tests.test_the_suite_runs_in_balanced_shards_sample_b"]
        out = io.StringIO()
        with mock.patch.object(RUNNER, "test_modules", lambda: modules), \
                mock.patch.object(RUNNER, "load_durations", lambda: {}), \
                mock.patch.object(RUNNER, "shard_argv",
                                  lambda shard, verbose: [sys.executable, "-c", SAMPLE_CHILD % (shard[0],)]):
            code = RUNNER.run_suite(2, False, out)

        said = out.getvalue()
        self.assertEqual(code, 1, said)
        self.assertEqual(re.findall(r"Ran (\d+) tests", said), ["7"])
        self.assertTrue(said.rstrip().endswith("FAILED (failures=1, skipped=1)"), said[-300:])
        self.assertIn("AssertionError: 'b' != 'c'", said)


class AShardThatHangsIsStoppedAndShown(unittest.TestCase):
    """The serial run printed each test as it ran, so a CI log showed where a
    hang was. The runner prints only when its shards are done; a hung shard
    would leave the job silent until GitHub's own six-hour limit."""

    def test_a_shard_past_its_time_is_stopped_and_fails_the_run_with_what_it_printed(self):
        ready = os.path.join(temp_dir(self), "printed")
        child = ("import time\n"
                 "print('the last thing it said', flush=True)\n"
                 f"open({ready!r}, 'w').close()\n"
                 "time.sleep(120)\n")

        # Until the child has got as far as printing before it is stopped: a
        # loaded machine can take longer than the first deadline to start it.
        for timeout in (2, 8, 30):
            code, output, seconds = RUNNER.run_shard([sys.executable, "-c", child], dict(os.environ),
                                                     timeout=timeout)
            if os.path.exists(ready):
                break
        self.assertTrue(os.path.exists(ready), "the child never got as far as printing")

        self.assertIsNone(code)
        self.assertIn("the last thing it said", output)
        self.assertIn(f"stopped after {timeout}s", output)
        self.assertLess(seconds, 110, "it waited for the shard instead of stopping it")
        ok, said = merged([["tests.test_a"]], [(code, output, seconds)])
        self.assertFalse(ok)
        self.assertIn("ERROR: shard 1 of 1 was stopped after", said)
        self.assertIn("the last thing it said", said)
        self.assertTrue(said.rstrip().endswith("FAILED (errors=1)"), said[-200:])

    def test_a_child_left_holding_the_output_does_not_hold_up_the_report(self):
        """A test that leaves a process of its own behind leaves it holding
        the shard's output. Read through a pipe, that output would not end
        until that process did; written to a file, it ends with the shard."""
        folder = temp_dir(self)
        release, done = os.path.join(folder, "release"), os.path.join(folder, "done")
        grandchild = ("import os, sys, time\n"
                      "deadline = time.time() + 60\n"
                      "while not os.path.exists(sys.argv[1]) and time.time() < deadline:\n"
                      "    time.sleep(0.05)\n"
                      "open(sys.argv[2], 'w').close()\n")
        child = ("import subprocess, sys\n"
                 f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, {release!r}, {done!r}],\n"
                 "                 stdout=sys.stdout.fileno(), stderr=sys.stderr.fileno())\n"
                 "print('the shard is done', flush=True)\n")

        def let_it_go():
            with open(release, "w"):
                pass
            for _ in range(600):
                if os.path.exists(done):
                    return
                time.sleep(0.1)
        self.addCleanup(let_it_go)
        with mock.patch.object(tempfile, "tempdir", folder):
            code, output, _seconds = RUNNER.run_shard([sys.executable, "-c", child], dict(os.environ), timeout=120)

        self.assertEqual(code, 0, output)
        self.assertIn("the shard is done", output)
        self.assertFalse(os.path.exists(done), "the report waited for the process the shard left behind")


class TheReportIsWrittenInUtf8(unittest.TestCase):
    """preflight reads the runner's report as UTF-8, which is what a test
    process writes once a test has imported oserve.py. Written in the
    console's code page instead (cp1253 on a Greek Windows), a Greek skip
    reason reached preflight as U+FFFD, and preflight died printing it."""

    def test_what_the_code_page_lacks_arrives_whole(self):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1253", errors="strict", newline="\n")

        def fake_run_suite(jobs, verbose, out, timeout):
            out.write("skipped 'den eparkei - δεν ٠ ✓'\n")
            out.flush()
            return 0
        with mock.patch.object(RUNNER, "run_suite", fake_run_suite), mock.patch.object(sys, "stdout", console):
            RUNNER.main([])

        self.assertIn("skipped 'den eparkei - δεν ٠ ✓'", raw.getvalue().decode("utf-8"))


class CIRunsItThisWay(unittest.TestCase):

    def test_the_suite_step_runs_the_parallel_runner_verbosely(self):
        with io.open(os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml"), encoding="utf-8") as handle:
            workflow = handle.read()
        step = workflow.split("- name: Run the regression suite", 1)[1]

        self.assertIn("\n        run: python scripts/run_tests_in_parallel.py -v\n", step)


TEMP_FACTORIES = {"NamedTemporaryFile", "mkstemp", "mkdtemp", "TemporaryDirectory", "TemporaryFile"}


class NoTestWritesIntoTheRepositoryRoot(unittest.TestCase):
    """The suite's scanners walk the repository root. Five tests wrote a
    temporary .py file there, and with the suite split across processes a
    scanner in another shard read one half-written or saw it vanish
    mid-scan: a FileNotFoundError naming a file nobody ships. They write
    into a temp folder now; this keeps it that way."""

    def test_no_temp_file_is_made_in_the_repository_root(self):
        import ast
        offenders = []
        tests_dir = os.path.join(REPO_ROOT, "tests")
        for name in sorted(os.listdir(tests_dir)):
            if not name.endswith(".py"):
                continue
            with io.open(os.path.join(tests_dir, name), encoding="utf-8") as handle:
                text = handle.read()
            if "dir=" not in text:
                continue
            for node in ast.walk(parse_source(text, filename=name)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called not in TEMP_FACTORIES:
                    continue
                for keyword in node.keywords:
                    value = keyword.value
                    named = value.attr if isinstance(value, ast.Attribute) else getattr(value, "id", "")
                    if keyword.arg == "dir" and named in ("REPO_ROOT", "ROOT", "SRC", "SRC_DIR"):
                        offenders.append(f"{name}:{node.lineno}")

        self.assertEqual(offenders, [], "write it into temp_dir(self) and point the scan there")


# Each child defines a tiny TestCase from its module name and runs it the way
# `python -m unittest` would, so the real runner and the real output are used.
SAMPLE_CHILD = r'''
import sys, unittest
name = %r
class Case(unittest.TestCase):
    def test_1(self): pass
    def test_2(self): pass
if name.endswith("_b"):
    def test_3(self): self.assertEqual("b", "c")
    Case.test_3 = test_3
else:
    @unittest.skip("a sample skip")
    def test_3(self): pass
    Case.test_3 = test_3
    def test_4(self): pass
    Case.test_4 = test_4
sys.modules["sample"] = sys.modules[__name__]
unittest.main(module=None, argv=["x", "__main__.Case"])
'''


if __name__ == "__main__":
    unittest.main()


class APathOnAnotherDrive(unittest.TestCase):
    """GitHub's Windows runners keep the checkout on D: and the temp folder
    on C:, where os.path.relpath() raises ValueError. --record's closing
    message said where it wrote by relpath() and failed the run there."""

    def test_a_path_relpath_cannot_reach_is_shown_whole(self):
        def across_drives(path, start=None):
            raise ValueError("path is on mount 'C:', start on mount 'D:'")
        with mock.patch.object(RUNNER.os.path, "relpath", across_drives):
            self.assertEqual(RUNNER.shown_path("C:/elsewhere/d.json"), "C:/elsewhere/d.json")

    def test_a_path_inside_the_repository_is_shown_relative(self):
        inside = os.path.join(RUNNER.REPO_ROOT, "tests", "module_durations.json")
        self.assertEqual(RUNNER.shown_path(inside), os.path.join("tests", "module_durations.json"))
