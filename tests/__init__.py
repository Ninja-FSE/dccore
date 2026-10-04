"""Regression tests for DCCore.

Run from the repository root with:

    python -m unittest discover -s tests -t .

Stdlib only, so this works on the production LXC and on Windows with nothing
installed. Each test names the defect it guards against; the suite exists so a
future change cannot silently reintroduce one of them.

NOTHING IN THIS SUITE MAY OPEN A REAL BROWSER.

webserver.start() opens the dashboard in the operator's browser, and two tests
in test_webserver.py drive start() directly to check the WEBUI_ENABLED and
WEBUI_HOST gates. Those tests already replace app.run() so that a broken gate
cannot bind a real socket - "a test must not be able to start a live listener
because the code it is testing broke" - but the browser call sits one line
above that, and was missed. Every full-suite run launched
http://127.0.0.1:8420/ on the developer's desktop, minutes apart, on a machine
where the bot itself was not running.

The guard lives here rather than in tests/support.py because only 90 of the
121 test files import that harness, and the class that tripped this is a plain
unittest.TestCase. Importing this package is the one thing every run does.

Calls are RECORDED rather than dropped, so a test can still assert that the
dashboard would have been opened - see BROWSER_OPENS.

EVERY DAEMON MODULE LIVES IN src/ now (#959) - one plain directory, not a
package, so the modules still only ever import each other by bare name,
never a package prefix, exactly as when they all sat at the repository
root. A test file's own "import irc" or "import defaults as config" needs
src/ on sys.path, and this is the one place that puts it there: importing
this package is the one thing every run does (see the browser guard's own
note above), so a test file never has to set this up itself - 345 of them
used to, identically, before this. One fact, one place, not repeated.
"""

import os as _os
import sys as _sys

_SRC = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "src")
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

import webbrowser as _webbrowser

# Every URL the suite tried to open, in order.
BROWSER_OPENS = []


def _record_browser_open(url, *_args, **_kwargs):
    BROWSER_OPENS.append(url)
    return True


_webbrowser.open = _record_browser_open
_webbrowser.open_new = _record_browser_open
_webbrowser.open_new_tab = _record_browser_open


# EVERY SHARD LISTENS ON PORTS OF ITS OWN (#1146).
#
# scripts/run_tests_in_parallel.py runs the suite in up to four processes at
# once. Every one of them would scan the same DCC ports: the shipped
# 55000-55010, and the ranges tests pin for themselves, one of which seven
# modules share. Two shards could then reach for one port at the same moment.
# On Linux both binds can succeed (SO_REUSEADDR, before either socket
# listens) and the second listen() fails; elsewhere the loser moves on to the
# next port, and a test that holds two ports, or counts the free ones, fails
# on a busy machine and passes on a quiet one.
#
# So the runner gives each shard a DCCORE_TEST_PORT_SHIFT of its own (1000,
# 2000, 3000 or 8000: see PORT_SHIFTS there), and every DCC port a test
# listens on moves by it:
#   - a range a test pins for itself is written as dcc_ports(start, end), and
#     moved here;
#   - the configured range, which every other test uses, is moved when this
#     package is imported, and again after each reload of defaults (a !rehash
#     test reloads it and gets the shipped numbers back).
# tests/test_every_shard_listens_on_ports_of_its_own.py proves the moved
# ranges of different shards never meet, and that none of them is the
# shipped 55000-55010 that check-setup's own children still probe. A plain
# run has no shift, and nothing below changes anything in it.
PORT_SHIFT = int(_os.environ.get("DCCORE_TEST_PORT_SHIFT") or 0)


def dcc_ports(start, end):
    """(start, end) of a DCC port range a test pins for itself, moved into this
    process's own window: unchanged in a plain run, moved by the shard's
    shift in a parallel one."""
    return start + PORT_SHIFT, end + PORT_SHIFT


def _move_the_configured_range(config):
    """Whatever range defaults resolved to, moved into this process's window."""
    config.DCC_PORT_START, config.DCC_PORT_END = dcc_ports(config.DCC_PORT_START,
                                                           config.DCC_PORT_END)


if PORT_SHIFT:
    import importlib as _importlib

    import defaults as _defaults

    _move_the_configured_range(_defaults)
    _plain_reload = _importlib.reload

    def _reload_into_the_shards_window(module):
        reloaded = _plain_reload(module)
        if getattr(reloaded, "__name__", None) == "defaults":
            _move_the_configured_range(reloaded)
        return reloaded

    _importlib.reload = _reload_into_the_shards_window


# A TEST LEAVES THE PROCESS AS IT FOUND IT.
#
# Each shard runs a hundred-odd modules in one process, and the CI flakes
# that kept being re-run were all one test reaching into a later one:
#   - a settings save in test_dashboard_routes started a real !rehash on a
#     thread nobody waited for; it reloaded defaults while the NEXT test was
#     logging in, cleared the password hash, and the login answered 401;
#   - every real DCC send left a 3, 15 or 45 s queue sweep behind, which woke
#     inside a later test and thawed, dropped or re-froze ITS queue;
#   - DCC CHAT listeners held a third of the shard's ports for a minute;
#   - settings set directly and never put back (the DCC port range collapsed
#     to one port, a password hash, a debug channel, two list folders).
# None of them failed the test that caused it, so each one cost a hunt.
#
# So every test, whatever its base class, is checked after its last cleanup:
#   - a thread it started that is still running fails it. One that is only
#     finishing gets LEAK_GRACE_SECONDS; a queue sweep, a rehash or a list
#     update fails at once, because ending a little later is exactly how
#     those reach the next test;
#   - a setting it changed and did not put back fails it - unless the value
#     left is the one defaults.py ships, which is what a test that reloads
#     defaults gets, and is no leak; or the value tests/support.py's
#     reset_config() gives it; or unless the test's class names it in
#     SETTINGS_THE_HARNESS_SETS (DCCoreTestCase: the ones its setUp resets
#     for every test anyway).
# It costs a snapshot of a dozen threads and the ~140 shipped settings per
# test; a grace wait only happens when something is still running.
#
# Hooked into unittest.TestCase._callSetUp, the one call every test makes
# before setUp (Python 3.8 onwards), so a plain TestCase is held to it as
# well as DCCoreTestCase - several of the leaks above were plain TestCases.
# tests/test_a_test_leaves_the_process_as_it_found_it.py proves the hook is
# still in place and still catches each kind.
import threading as _threading
import time as _time
import unittest as _unittest

LEAK_GRACE_SECONDS = 5.0

# Threads that do their harm by ending late: alive after the cleanups at all
# is already a leak, however soon they would finish.
ACTS_ON_A_LATER_TEST = frozenset((
    "delayed_queue_trigger_fallback", "delayed_port_retry",  # dcc.start_dcc_send()
    "handle_rehash_request",                                 # a settings save's !rehash
    "async_list_updater",                                    # a list update request
))

_UNSET = object()


def _settings():
    """{name: value} for every setting defaults.py ships, or None before the
    daemon's config has been imported at all."""
    config = _sys.modules.get("defaults")
    shipped = getattr(config, "SHIPPED_VALUES", None)
    if not isinstance(shipped, dict):
        return None
    return {name: getattr(config, name, _UNSET) for name in shipped}


def _same(left, right):
    try:
        return left is right or bool(left == right)
    except Exception:  # noqa: BLE001 - a value that cannot compare is not the same
        return False


def _target_name(thread):
    target = getattr(thread, "_target", None)
    return getattr(target, "__name__", None) or thread.name


def _check_the_test_left_the_process_as_it_found_it(test, found):
    problems = []

    new = [thread for thread in _threading.enumerate()
           if thread not in found["threads"] and thread.is_alive()]
    late = [thread for thread in new if _target_name(thread) in ACTS_ON_A_LATER_TEST]
    deadline = _time.monotonic() + LEAK_GRACE_SECONDS
    for thread in new:
        if thread not in late:
            thread.join(max(0.0, deadline - _time.monotonic()))
    left = late + [thread for thread in new if thread not in late and thread.is_alive()]
    if left:
        problems.append(
            "threads this test started are still running after its cleanups: %s. "
            "Join them in a cleanup, or stub what starts them (a real DCC send: "
            "tests.support.hold_send_follow_ups)." % ", ".join(
                sorted(repr(_target_name(thread)) for thread in left)))

    before = found["settings"]
    after = _settings()
    if before is not None and after is not None:
        shipped = getattr(_sys.modules.get("defaults"), "SHIPPED_VALUES", {})
        theirs = getattr(test, "SETTINGS_THE_HARNESS_SETS", ())
        support = _sys.modules.get("tests.support") or _sys.modules.get("support")
        reset = getattr(support, "SETTINGS_RESET_VALUES", {})
        changed = ["%s: %r -> %r" % (name, before[name], after[name])
                   for name in sorted(before)
                   if name in after and name not in theirs
                   and not _same(before[name], after[name])
                   and not _same(after[name], shipped.get(name, _UNSET))
                   and not _same(after[name], reset.get(name, _UNSET))]
        if changed:
            problems.append(
                "settings this test changed and did not put back: %s. Use "
                "set_config() (DCCoreTestCase) or addCleanup(setattr, config, "
                "name, old_value)." % "; ".join(changed))

    if problems:
        raise AssertionError("This test leaks into the tests after it. " + " ".join(problems))


_plain_call_set_up = _unittest.TestCase._callSetUp


def _call_set_up_and_check_afterwards(self):
    found = {"threads": set(_threading.enumerate()), "settings": _settings()}
    # The first cleanup registered, so the last to run.
    self.addCleanup(_check_the_test_left_the_process_as_it_found_it, self, found)
    return _plain_call_set_up(self)


_unittest.TestCase._callSetUp = _call_set_up_and_check_afterwards
