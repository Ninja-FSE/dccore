#!/usr/bin/env python3
"""Run the regression suite in a few processes at once, and report it as one run.

    python scripts/run_tests_in_parallel.py           # what CI runs
    python scripts/run_tests_in_parallel.py -v        # one line per test
    python scripts/run_tests_in_parallel.py -j 2      # two processes
    python scripts/run_tests_in_parallel.py --record  # re-measure the timings

The suite is about 450 modules and ran in one process: five to ten minutes,
and preflight runs it more than once (#1146). Here the modules are split into
up to four shards, each run by its own `python -m unittest <modules>` process,
and the shards are balanced by how long each module took when last measured
(tests/module_durations.json): longest first, each onto the shard with the
least time so far. A module nobody has measured yet counts as the median.
A stale table costs balance, never a test.

Each shard runs its modules in alphabetical order, as the serial run does,
so a shard is the serial run with modules taken out, never reordered. A
shard that fails prints the one command that runs it again.

Each shard also listens on DCC ports of its own: shard i is started with
DCCORE_TEST_PORT_SHIFT set to PORT_SHIFTS[i], and tests/__init__.py moves
every port a test listens on by that much. Two shards therefore never listen
on the same port, and none listens in the shipped 55000-55010 at all. Four
shards at most, one per window; tests/test_every_shard_listens_on_ports_of_its_own.py
checks that the windows still stay apart.

The output reads as one unittest run - every shard's progress lines, then
every shard's failures, then one summary with the exact total - so preflight
and CI read "Ran N tests" and "skipped=N" from it as they read a serial run.
The exit status is 0 only if every shard passed. A shard that runs past
--timeout is stopped and reported with what it had printed, rather than
leaving a CI job silent until the job's own limit.

Standard library only, like the suite. It ships, unlike preflight.py: CI runs it.
"""

import argparse
import io
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
DURATIONS_FILE = os.path.join(TESTS_DIR, "module_durations.json")

# How far each shard's DCC ports move from the ones a plain run uses
# (tests/__init__.py does the moving). The ranges the tests pin sit in two
# clusters, 51000-51370 and 55000-55650; a move of 1000, 2000 or 3000 keeps
# every copy apart. 4000 would put the 51000s onto 55000-55010, which stays
# out of every shard: check-setup's own children probe it in each of them,
# and an operator's daemon on the same machine listens there. 8000 is the
# next move that clears everything.
PORT_SHIFTS = (1000, 2000, 3000, 8000)
PORT_SHIFT_VARIABLE = "DCCORE_TEST_PORT_SHIFT"

# GitHub's runners have 3 or 4 CPUs, and past four the longest module sets
# the floor anyway. One shard per port window, so never more than those.
DEFAULT_JOBS = 4
MAX_JOBS = len(PORT_SHIFTS)

# Generous: a whole serial run takes ten minutes on a slow runner. Only a
# hang reaches it, and a hang is better reported than waited out.
DEFAULT_TIMEOUT = 1800

# A unittest run ends with a rule, the count and the time, a blank line,
# then OK or FAILED and what it counted.
SUMMARY = re.compile(
    r"\n-{70}\nRan (?P<ran>\d+) tests? in (?P<seconds>[\d.]+)s\n\n"
    r"(?P<verdict>OK|FAILED)(?: \((?P<counts>[^)\n]*)\))?[^\n]*\n?")
# The order unittest itself prints them in.
COUNT_NAMES = ("failures", "errors", "skipped", "expected failures", "unexpected successes")
RULE = "=" * 70 + "\n"

# Python 3.13 and later colour unittest's output when FORCE_COLOR or
# PYTHON_COLORS=1 asks for it, even into a file: the summary then reads
# "\x1b[32mOK\x1b[0m", SUMMARY does not match, and a shard that passed
# reads as one that never finished. shard_env() asks every shard for plain
# output, and any escape that still arrives (an interpreter that ignores
# the variables) is taken out before the output is read.
NO_COLOUR = {"PYTHON_COLORS": "0", "NO_COLOR": "1"}
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def test_modules():
    """Every module discover would load: tests/test*.py, as dotted names."""
    names = [name[:-3] for name in os.listdir(TESTS_DIR)
             if name.startswith("test") and name.endswith(".py")]
    return sorted("tests." + name for name in names)


def load_durations(path=DURATIONS_FILE):
    try:
        with io.open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def make_shards(modules, durations, count):
    """`count` lists of modules, each in alphabetical order, balanced by
    duration: longest module first, each onto the least loaded shard."""
    known = [durations[m] for m in modules if m in durations]
    default = statistics.median(known) if known else 1.0
    weight = {m: durations.get(m, default) for m in modules}
    shards = [[] for _ in range(max(1, count))]
    loads = [0.0] * len(shards)
    for module in sorted(modules, key=lambda m: (-weight[m], m)):
        lightest = loads.index(min(loads))
        shards[lightest].append(module)
        loads[lightest] += weight[module]
    return [sorted(shard) for shard in shards]


def shard_env(index, base=None):
    """The environment shard `index` runs in: the caller's, plus its port
    shift, with colour turned off (see NO_COLOUR)."""
    env = dict(os.environ if base is None else base)
    env.pop("FORCE_COLOR", None)
    env.update(NO_COLOUR)
    env[PORT_SHIFT_VARIABLE] = str(PORT_SHIFTS[index])
    return env


def split_summary(output):
    """(body, ran, verdict, {count name: n}) of one shard's output, or
    (output, None, None, {}) when the run never got as far as a summary.

    The LAST summary counts. Anything printed after it - a thread that
    outlived the run - is kept in the body rather than lost. Colour escapes
    are dropped first, from the body too (see NO_COLOUR)."""
    output = ANSI_ESCAPE.sub("", output)
    matches = list(SUMMARY.finditer(output))
    if not matches:
        return output, None, None, {}
    match = matches[-1]
    counts = {}
    for part in (match.group("counts") or "").split(", "):
        if "=" in part:
            name, value = part.split("=", 1)
            counts[name.strip()] = int(value)
    body = output[:match.start()] + "\n" + output[match.end():]
    return body, int(match.group("ran")), match.group("verdict"), counts


def split_failures(body):
    """(progress, failures): the dots or verbose lines, and the ===== blocks
    unittest prints after them."""
    at = body.find(RULE)
    if at < 0:
        return body.rstrip("\n") + "\n", ""
    return body[:at].rstrip("\n") + "\n", body[at:].rstrip("\n") + "\n"


def shard_argv(modules, verbose):
    return [sys.executable, "-m", "unittest"] + (["-v"] if verbose else []) + list(modules)


def run_shard(argv, env, timeout=DEFAULT_TIMEOUT):
    """(exit status, output, seconds); exit status None if it timed out.

    unittest writes its report to stderr and the tests print to stdout; one
    file keeps them in order. A file rather than a pipe: a child a test
    started may still hold the pipe, and reading it would then wait for
    that child too, which is the very hang the timeout is there to end."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="dccore-shard-", ignore_cleanup_errors=True) as folder:
        path = os.path.join(folder, "output.txt")
        with open(path, "wb") as sink:
            child = subprocess.Popen(argv, cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
                                     stdout=sink, stderr=subprocess.STDOUT)
            try:
                code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                code = None
        # Read back as text mode would have: Windows ends the lines with
        # \r\n, and the summary is found by its \n line ends.
        with open(path, "r", encoding="utf-8", errors="replace", newline=None) as handle:
            output = handle.read()
    if code is None:
        output += (f"\n[run_tests_in_parallel] stopped after {timeout:.0f}s: the shard "
                   f"was still running.\n")
    return code, output, time.monotonic() - started


def report(shards, finished, elapsed, out):
    """Write the shards' results as one unittest report; True if all passed."""
    parsed = [split_summary(output) for _code, output, _seconds in finished]
    split = [split_failures(body) for body, _ran, _verdict, _counts in parsed]

    for progress, _failures in split:
        out.write(progress)
    out.write("\n")
    for _progress, failures in split:
        out.write(failures)

    broken = 0
    for number, ((code, output, seconds), (_body, ran, verdict, _counts)) in enumerate(
            zip(finished, parsed), 1):
        if code is None or ran is None or (verdict == "OK") != (code == 0):
            broken += 1
            out.write(RULE)
            if code is None:
                out.write(f"ERROR: shard {number} of {len(shards)} was stopped after "
                          f"{seconds:.0f}s, still running. Its last output:\n")
            else:
                out.write(f"ERROR: shard {number} of {len(shards)} ended without a summary "
                          f"of its own (exit status {code}). Its last output:\n")
            out.write("-" * 70 + "\n")
            out.write(output[-4000:].rstrip("\n") + "\n\n")

    totals = {}
    for _body, _ran, _verdict, counts in parsed:
        for name, value in counts.items():
            totals[name] = totals.get(name, 0) + value
    # A shard that never reached its summary is one error, so it cannot pass.
    totals["errors"] = totals.get("errors", 0) + broken
    ok = all(verdict == "OK" for _body, _ran, verdict, _counts in parsed) and not broken

    for number, (shard, (code, _output, seconds), (_body, ran, verdict, _counts)) in enumerate(
            zip(shards, finished, parsed), 1):
        state = "stopped, still running" if code is None else (verdict or "no summary")
        out.write(f"shard {number}/{len(shards)}: {len(shard)} modules, "
                  f"{'?' if ran is None else ran} tests, {seconds:.1f}s, {state}\n")
        if code is None or verdict != "OK":
            out.write("    run it again: python -m unittest " + " ".join(shard) + "\n")

    total = sum(ran or 0 for _body, ran, _verdict, _counts in parsed)
    infos = [f"{name}={totals[name]}" for name in COUNT_NAMES if totals.get(name)]
    out.write("-" * 70 + "\n")
    out.write(f"Ran {total} test{'' if total == 1 else 's'} in {elapsed:.3f}s\n\n")
    out.write(("OK" if ok else "FAILED") + (f" ({', '.join(infos)})" if infos else "") + "\n")
    out.flush()
    return ok


def run_all(argvs, timeout):
    """Run up to MAX_JOBS commands at once, the i-th in port window i, so no
    two of them ever share a port. Results in the order given."""
    if len(argvs) > MAX_JOBS:
        raise ValueError(f"{len(argvs)} at once, but there are {MAX_JOBS} port windows")
    results = [None] * len(argvs)

    def one(index):
        results[index] = run_shard(argvs[index], shard_env(index), timeout)

    threads = [threading.Thread(target=one, args=(index,), daemon=True)
               for index in range(len(argvs))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def run_suite(jobs, verbose, out, timeout=DEFAULT_TIMEOUT):
    shards = [s for s in make_shards(test_modules(), load_durations(), jobs) if s]
    started = time.monotonic()
    finished = run_all([shard_argv(shard, verbose) for shard in shards], timeout)
    return 0 if report(shards, finished, time.monotonic() - started, out) else 1


def shown_path(path):
    """`path` relative to the repository when it can be, for a message.

    On Windows relpath() raises ValueError for a path on another drive - on
    GitHub's runners the checkout is on D: and the temp folder on C: - and a
    message is no reason to fail the run, so that path is shown whole."""
    try:
        return os.path.relpath(path, REPO_ROOT)
    except ValueError:
        return path


def record(jobs, out, timeout=DEFAULT_TIMEOUT):
    """Time every module in a process of its own and rewrite the table."""
    modules = test_modules()
    results = []
    # In batches of `jobs`, so every module runs while exactly as many others
    # do as in a real run, each batch member in a port window of its own.
    for at in range(0, len(modules), jobs):
        batch = modules[at:at + jobs]
        for module, (code, _output, seconds) in zip(batch, run_all(
                [shard_argv([m], False) for m in batch], timeout)):
            results.append((module, seconds))
            out.write(f"{seconds:7.1f}s  {module}{'' if code == 0 else '  (failed on its own)'}\n")
            out.flush()
    with io.open(DURATIONS_FILE, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({module: round(seconds, 1) for module, seconds in results}, handle,
                  indent=1, sort_keys=True)
        handle.write("\n")
    out.write(f"wrote {len(results)} timings to {shown_path(DURATIONS_FILE)}\n")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("-j", "--jobs", type=int, default=min(DEFAULT_JOBS, os.cpu_count() or 1),
                        help=f"how many processes, at most {MAX_JOBS} (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="one line per test")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds a shard may run before it is stopped (default: %(default)s)")
    parser.add_argument("--record", action="store_true",
                        help="time each module on its own and rewrite tests/module_durations.json")
    args = parser.parse_args(argv)
    # The shards' output carries whatever the tests printed - Greek, emoji -
    # so it goes out as UTF-8, which is what a test process itself writes
    # once a test has imported oserve.py, and what preflight decodes it as.
    # Left in the console's code page (cp1253 on a Greek Windows), a Greek
    # skip reason reached preflight as U+FFFD and preflight then died
    # printing it. Anything still unwritable is escaped, as unittest's own
    # stderr does it.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    jobs = max(1, min(args.jobs, MAX_JOBS))
    if args.record:
        return record(jobs, sys.stdout, args.timeout)
    return run_suite(jobs, args.verbose, sys.stdout, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
