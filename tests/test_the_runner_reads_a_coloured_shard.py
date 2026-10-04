"""The parallel runner failed a fully passing suite when colour was asked for.

Python 3.13 and later colour unittest's output when FORCE_COLOR or
PYTHON_COLORS=1 is set, even when it goes to a file. A shard's summary then
ended "\\x1b[32mOK\\x1b[0m", scripts/run_tests_in_parallel.py's SUMMARY did not
match it, and every shard was reported as one that "ended without a summary
of its own": "Ran 0 tests", FAILED, exit status 1, with every test passing.
A developer whose shell exports FORCE_COLOR=1 (common for node tooling), or
a CI runner set up with it, got a red suite and a preflight saying "only 0
collected" - a message about missing modules, not about colour.

Two guards, each held here: shard_env() turns colour off for every shard
(FORCE_COLOR dropped, PYTHON_COLORS=0, NO_COLOR=1), and split_summary()
drops escape sequences before it reads anything, for an interpreter that
colours anyway. The parsing tests build the coloured text as Python 3.14
writes it, so they run on every Python in CI's matrix, not only on the ones
that colour; the end-to-end tests then run a real unittest child.
"""

import importlib.util
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_tests_in_parallel_colour_under_test",
        os.path.join(REPO_ROOT, "scripts", "run_tests_in_parallel.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = load_runner()

# The escapes Python 3.14's _colorize theme gives unittest.
GREEN, YELLOW, BOLD_RED, RESET = "\x1b[32m", "\x1b[33m", "\x1b[1;31m", "\x1b[0m"


def coloured_output(ran, verdict, infos=()):
    """A shard's output as a colouring unittest writes it: coloured progress
    dots, then the rule, the count, and a coloured verdict and counts."""
    colour = GREEN if verdict == "OK" else BOLD_RED
    summary = f"{colour}{verdict}{RESET}"
    if infos:
        summary += " (" + ", ".join(f"{c}{text}{RESET}" for c, text in infos) + ")"
    return ((f"{GREEN}.{RESET}" * ran) + "\n" + "-" * 70 + "\n"
            + f"Ran {ran} test{'' if ran == 1 else 's'} in 1.033s\n\n" + summary + "\n")


class AColouredSummaryIsRead(unittest.TestCase):

    def test_a_coloured_ok(self):
        _body, ran, verdict, counts = RUNNER.split_summary(coloured_output(29, "OK"))

        self.assertEqual((ran, verdict, counts), (29, "OK", {}))

    def test_a_coloured_ok_with_its_skips(self):
        output = coloured_output(5, "OK", [(YELLOW, "skipped=2")])

        _body, ran, verdict, counts = RUNNER.split_summary(output)

        self.assertEqual((ran, verdict, counts), (5, "OK", {"skipped": 2}))

    def test_a_coloured_failed_with_its_counts(self):
        output = coloured_output(7, "FAILED", [(BOLD_RED, "failures=1"), (BOLD_RED, "errors=2"),
                                               (YELLOW, "skipped=1")])

        _body, ran, verdict, counts = RUNNER.split_summary(output)

        self.assertEqual((ran, verdict, counts),
                         (7, "FAILED", {"failures": 1, "errors": 2, "skipped": 1}))

    def test_the_progress_is_passed_on_without_its_escapes(self):
        body, _ran, _verdict, _counts = RUNNER.split_summary(coloured_output(3, "OK"))

        self.assertNotIn("\x1b", body)
        self.assertIn("...", body)

    def test_coloured_shards_that_all_passed_make_a_passing_run(self):
        shards = [["tests.test_a"], ["tests.test_b"]]
        finished = [(0, coloured_output(29, "OK"), 1.0),
                    (0, coloured_output(4, "OK", [(YELLOW, "skipped=1")]), 1.0)]
        out = io.StringIO()

        ok = RUNNER.report(shards, finished, 2.0, out)

        said = out.getvalue()
        self.assertTrue(ok, said)
        self.assertIn("Ran 33 tests in 2.000s\n\nOK (skipped=1)\n", said)
        self.assertNotIn("without a summary", said)


class EveryShardIsAskedForPlainOutput(unittest.TestCase):

    def test_colour_asked_for_by_the_caller_is_turned_off(self):
        env = RUNNER.shard_env(0, base={"PATH": "x", "FORCE_COLOR": "1", "PYTHON_COLORS": "1"})

        self.assertNotIn("FORCE_COLOR", env)
        self.assertEqual(env["PYTHON_COLORS"], "0")
        self.assertEqual(env["NO_COLOR"], "1")
        self.assertEqual(env["PATH"], "x")

    def test_and_with_no_colour_asked_for_it_stays_off(self):
        env = RUNNER.shard_env(3, base={})

        self.assertEqual((env.get("PYTHON_COLORS"), env.get("NO_COLOR")), ("0", "1"))
        self.assertNotIn("FORCE_COLOR", env)


# A real unittest run, as a shard runs one: two tests, the second passing
# or failing as the parent asks.
CHILD = r'''
import unittest
class Case(unittest.TestCase):
    def test_1(self): pass
    def test_2(self): %s
unittest.main(module=None, argv=["x", "__main__.Case"])
'''


def colour_asked_for():
    """The caller's environment with colour forced on, as FORCE_COLOR=1 in a
    developer's shell would leave it."""
    env = dict(os.environ, FORCE_COLOR="1", PYTHON_COLORS="1")
    env.pop("NO_COLOR", None)
    return env


class ARealShardRunWithColourAskedFor(unittest.TestCase):

    def run_child(self, body, env):
        code, output, _seconds = RUNNER.run_shard([sys.executable, "-c", CHILD % body], env, timeout=120)
        return code, output

    def test_a_passing_shard_reads_as_ok_and_prints_no_escapes(self):
        code, output = self.run_child("pass", RUNNER.shard_env(0, base=colour_asked_for()))

        self.assertEqual(code, 0, output)
        self.assertNotIn("\x1b", output)
        _body, ran, verdict, _counts = RUNNER.split_summary(output)
        self.assertEqual((ran, verdict), (2, "OK"), output)

    def test_a_failing_shard_reads_as_failed(self):
        code, output = self.run_child("self.assertEqual(1, 2)", RUNNER.shard_env(1, base=colour_asked_for()))

        self.assertEqual(code, 1, output)
        _body, ran, verdict, counts = RUNNER.split_summary(output)
        self.assertEqual((ran, verdict, counts), (2, "FAILED", {"failures": 1}), output)

    def test_an_interpreter_that_colours_anyway_is_still_read(self):
        """The second guard against the real thing: colour left on, as an
        interpreter that ignores the variables would print it. Only a Python
        that colours unittest (3.13 and later) can show it; on the others the
        parsing tests above stand in for it."""
        code, output = self.run_child("pass", colour_asked_for())
        if "\x1b[" not in output:
            self.skipTest("this Python does not colour unittest's output")

        self.assertEqual(code, 0, output)
        _body, ran, verdict, _counts = RUNNER.split_summary(output)
        self.assertEqual((ran, verdict), (2, "OK"), output)


if __name__ == "__main__":
    unittest.main()
