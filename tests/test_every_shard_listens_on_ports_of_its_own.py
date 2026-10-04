"""Every shard of a parallel run listens on DCC ports of its own (#1146).

scripts/run_tests_in_parallel.py runs the suite in up to four processes at
once. All four used to scan the same ports: the shipped 55000-55010, which
every test without a range of its own uses, and the ranges tests pin for
themselves - one of them, 51300-51310, shared by seven modules. Two shards
could reach for one port at the same moment. On Linux both binds can succeed
(SO_REUSEADDR, before either socket listens) and one listen() then fails; on
Windows and macOS the loser moves on to the next port, and a test that holds
two ports, or counts the free ones, fails only on a busy machine. A CI flake
waiting to happen, however many local runs came back clean.

Now each shard runs with a DCCORE_TEST_PORT_SHIFT of its own (the runner's
PORT_SHIFTS), and tests/__init__.py moves every DCC port a test listens on by
that much: the configured range, when the package is imported and after every
reload of defaults, and each range a test pins, written as
tests.dcc_ports(start, end). These tests prove each piece, and that the moved
windows of two shards never share a port:

* the runner hands each process at once a different shift, and never more
  processes than there are windows;
* a process with a shift has its configured range and its pinned ranges
  moved, and keeps them moved across a reload; one without is untouched;
* four real processes started by the runner report windows that do not meet;
* no test pins a range around dcc_ports(), and the ranges pinned through it
  stay apart in every shard - and out of the shipped 55000-55010, which
  check-setup's own children (separate processes, with no shift) probe in
  every shard, and which an operator's daemon on the same machine holds.
"""

import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402

from tests.support import parse_source, temp_dir  # noqa: E402

TESTS_DIR = os.path.join(REPO_ROOT, "tests")


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_tests_in_parallel_ports_under_test", os.path.join(REPO_ROOT, "scripts", "run_tests_in_parallel.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = load_runner()

# What a child reports: its configured range before and after a reload of
# defaults, and where one pinned range lands.
REPORT_THE_PORTS = (
    "import importlib, json, tests, defaults\n"
    "before = [defaults.DCC_PORT_START, defaults.DCC_PORT_END]\n"
    "importlib.reload(defaults)\n"
    "after = [defaults.DCC_PORT_START, defaults.DCC_PORT_END]\n"
    "print(json.dumps({'before': before, 'after': after,\n"
    "                  'pinned': list(tests.dcc_ports(51300, 51310))}))\n")


def child_env(test, shift=None):
    """The test process's environment, with no settings file of the
    machine's own to change the range, and the given shift or none."""
    env = dict(os.environ)
    env["DCCORE_SETTINGS_FILE"] = os.path.join(temp_dir(test), "settings.conf")
    env.pop(RUNNER.PORT_SHIFT_VARIABLE, None)
    if shift is not None:
        env[RUNNER.PORT_SHIFT_VARIABLE] = str(shift)
    return env


def ports_seen(env):
    done = subprocess.run([sys.executable, "-c", REPORT_THE_PORTS], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise AssertionError(done.stderr[-1500:])
    return json.loads(done.stdout.strip().splitlines()[-1])


SHIPPED = (config.SHIPPED_VALUES["DCC_PORT_START"], config.SHIPPED_VALUES["DCC_PORT_END"])


class TheRunnerGivesEachProcessItsOwnShift(unittest.TestCase):

    def test_each_window_has_its_own_shift(self):
        shifts = [RUNNER.shard_env(index, base={})[RUNNER.PORT_SHIFT_VARIABLE]
                  for index in range(RUNNER.MAX_JOBS)]

        self.assertEqual(shifts, [str(shift) for shift in RUNNER.PORT_SHIFTS])
        self.assertEqual(len(set(shifts)), RUNNER.MAX_JOBS)
        self.assertNotIn("0", shifts, "a shard listening where a plain run does")

    def test_the_rest_of_the_environment_is_passed_on(self):
        env = RUNNER.shard_env(2, base={"PATH": "x", RUNNER.PORT_SHIFT_VARIABLE: "7"})

        # Plus the two variables that keep a shard's summary free of colour
        # escapes (tests/test_the_runner_reads_a_coloured_shard.py).
        self.assertEqual(env, {"PATH": "x", RUNNER.PORT_SHIFT_VARIABLE: str(RUNNER.PORT_SHIFTS[2]),
                               "PYTHON_COLORS": "0", "NO_COLOR": "1"})

    def test_more_at_once_than_there_are_windows_is_refused(self):
        with self.assertRaises(ValueError):
            RUNNER.run_all([["x"]] * (RUNNER.MAX_JOBS + 1), 5)

    def test_asking_for_more_processes_gets_four(self):
        asked = []
        with mock.patch.object(RUNNER, "run_suite", lambda jobs, *_a: asked.append(jobs) or 0), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            RUNNER.main(["-j", "9"])
            RUNNER.main(["-j", "2"])

        self.assertEqual(asked, [RUNNER.MAX_JOBS, 2])
        self.assertEqual(RUNNER.MAX_JOBS, len(RUNNER.PORT_SHIFTS))

    def test_a_recording_runs_no_more_at_once_than_there_are_windows(self):
        batches = []

        def fake_run_all(argvs, timeout):
            batches.append(len(argvs))
            return [(0, "", 0.1)] * len(argvs)
        modules = [f"tests.test_{n:02d}" for n in range(10)]
        with mock.patch.object(RUNNER, "run_all", fake_run_all), \
                mock.patch.object(RUNNER, "test_modules", lambda: modules), \
                mock.patch.object(RUNNER, "DURATIONS_FILE", os.path.join(temp_dir(self), "d.json")):
            RUNNER.record(4, io.StringIO())

        self.assertEqual(batches, [4, 4, 2])


class AProcessMovesItsPorts(unittest.TestCase):

    def test_a_plain_run_is_untouched(self):
        seen = ports_seen(child_env(self))

        self.assertEqual(seen["before"], list(SHIPPED))
        self.assertEqual(seen["after"], list(SHIPPED))
        self.assertEqual(seen["pinned"], [51300, 51310])

    def test_a_shard_moves_the_configured_range_and_keeps_it_moved_across_a_reload(self):
        seen = ports_seen(child_env(self, shift=8000))

        moved = [SHIPPED[0] + 8000, SHIPPED[1] + 8000]
        self.assertEqual(seen["before"], moved)
        self.assertEqual(seen["after"], moved, "a reload of defaults put the shared range back")
        self.assertEqual(seen["pinned"], [59300, 59310])


class FourRealShardsNeverMeet(unittest.TestCase):
    """Through the runner's own machinery: four processes at once, as a real
    run starts them, each reporting the ports it would listen on."""

    def test_their_windows_are_apart(self):
        argvs = [[sys.executable, "-c", REPORT_THE_PORTS]] * RUNNER.MAX_JOBS
        env = child_env(self)
        with mock.patch.dict(os.environ, {"DCCORE_SETTINGS_FILE": env["DCCORE_SETTINGS_FILE"]}):
            results = RUNNER.run_all(argvs, 120)

        owners = {port: "the shipped range" for port in range(SHIPPED[0], SHIPPED[1] + 1)}
        for index, (code, output, _seconds) in enumerate(results):
            self.assertEqual(code, 0, output[-1500:])
            seen = json.loads(output.strip().splitlines()[-1])
            for start, end in (seen["before"], seen["after"], seen["pinned"]):
                for port in range(start, end + 1):
                    self.assertEqual(owners.setdefault(port, index), index,
                                     f"port {port} is in the windows of {owners[port]} and shard {index}")
        self.assertEqual(len(set(owners.values())), RUNNER.MAX_JOBS + 1)


def pinned_ranges_and_bypasses():
    """(every dcc_ports(start, end) call with literal numbers, as (module, line,
    start, end); every range pinned around it, as "module:line what")."""
    pinned, bypasses = [], []
    for name in sorted(os.listdir(TESTS_DIR)):
        if not name.endswith(".py"):
            continue
        with io.open(os.path.join(TESTS_DIR, name), encoding="utf-8") as handle:
            text = handle.read()
        if "PORT" not in text:
            continue
        found_pins, found_bypasses = scan(text, name)
        pinned += found_pins
        bypasses += found_bypasses
    return pinned, bypasses


# Ranges that are set with literal numbers on purpose and never listened on.
NOTHING_IS_BOUND = {
    ("test_dcc_fetch.py", "FetchListenerPortOrderingTests"):
        "fakes the socket layer to read the probe order; nothing binds",
}


def _literal_port(node):
    return (isinstance(node, ast.Constant) and isinstance(node.value, int)
            and not isinstance(node.value, bool) and node.value > 0)


def scan(text, name):
    tree = parse_source(text, filename=name)
    pinned, bypasses = [], []

    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "dcc_ports"
                and len(node.args) == 2 and all(_literal_port(a) for a in node.args)):
            pinned.append((name, node.lineno, node.args[0].value, node.args[1].value))

    # Module constants such as PORT_START = 51300.
    for node in tree.body:
        if isinstance(node, ast.Assign) and _literal_port(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.endswith(("PORT_START", "PORT_END")):
                    bypasses.append(f"{name}:{node.lineno} {target.id} = {node.value.value}")

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            here = child.name if isinstance(child, ast.ClassDef) else owner
            if (name, here) not in NOTHING_IS_BOUND:
                if isinstance(child, ast.keyword) and child.arg in ("DCC_PORT_START", "DCC_PORT_END") \
                        and _literal_port(child.value):
                    bypasses.append(f"{name}:{child.value.lineno} {child.arg}={child.value.value}")
                if isinstance(child, ast.Assign) and _literal_port(child.value):
                    for target in child.targets:
                        if isinstance(target, ast.Attribute) and target.attr in ("DCC_PORT_START", "DCC_PORT_END"):
                            bypasses.append(f"{name}:{child.lineno} .{target.attr} = {child.value.value}")
            visit(child, here)
    visit(tree, None)
    return pinned, bypasses


class EveryPinnedRangeMovesWithItsShard(unittest.TestCase):

    def test_no_test_pins_a_range_around_dcc_ports(self):
        _pinned, bypasses = pinned_ranges_and_bypasses()

        self.assertEqual(bypasses, [],
                         "write the range as tests.dcc_ports(start, end), so that a parallel run "
                         "moves it into each shard's own window")

    def test_the_moved_ranges_of_two_shards_never_share_a_port(self):
        pinned, _bypasses = pinned_ranges_and_bypasses()
        ranges = sorted({(start, end) for _name, _line, start, end in pinned} | {SHIPPED})
        self.assertGreater(len(ranges), 5, "the scan found too few ranges to mean anything")

        # The shipped range belongs to no shard: check-setup's children probe
        # it in all of them, with no shift.
        owners = {port: "the shipped range" for port in range(SHIPPED[0], SHIPPED[1] + 1)}
        clashes = []
        for index, shift in enumerate(RUNNER.PORT_SHIFTS):
            for start, end in ranges:
                self.assertLessEqual(end + shift, 65535)
                self.assertGreaterEqual(start + shift, 1024)
                for port in range(start + shift, end + shift + 1):
                    if owners.setdefault(port, f"shard {index}") != f"shard {index}":
                        clashes.append(f"{port}: {owners[port]} and shard {index}")

        self.assertEqual(clashes[:5], [], "pick a range whose copies stay apart from the others'")

    def test_the_scan_sees_each_shape(self):
        """Control: one of each way a range has been pinned, around dcc_ports()
        and through it."""
        sample = ("PORT_START = 51300\n"
                  "PORT_END = 51310\n"
                  "A, B = dcc_ports(52000, 52010)\n"
                  "class Case:\n"
                  "    def setUp(self):\n"
                  "        self.set_config(DCC_PORT_START=55630, DCC_PORT_END=0)\n"
                  "        config.DCC_PORT_END = 55199\n"
                  "        config.DCC_PORT_START = taken\n")

        pinned, bypasses = scan(sample, "sample.py")

        self.assertEqual(pinned, [("sample.py", 3, 52000, 52010)])
        self.assertEqual(bypasses, ["sample.py:1 PORT_START = 51300", "sample.py:2 PORT_END = 51310",
                                    "sample.py:6 DCC_PORT_START=55630", "sample.py:7 .DCC_PORT_END = 55199"])

    def test_an_exempt_class_is_exempt_only_for_its_own_lines(self):
        sample = ("class FetchListenerPortOrderingTests:\n"
                  "    def test(self):\n"
                  "        self.set_config(DCC_PORT_START=55000, DCC_PORT_END=55010)\n"
                  "class Other:\n"
                  "    def test(self):\n"
                  "        self.set_config(DCC_PORT_START=55000)\n")

        _pinned, bypasses = scan(sample, "test_dcc_fetch.py")

        self.assertEqual(bypasses, ["test_dcc_fetch.py:6 DCC_PORT_START=55000"])


if __name__ == "__main__":
    unittest.main()
