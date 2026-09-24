"""No module `!rehash` reloads may construct its own module-level lock.

WHY THIS EXISTS

#235 fixed four of them. `importlib.reload()` re-executes a module body, so
`queue_lock = threading.Lock()` at module level is rebound on every rehash -
a thread already inside `with dcc.queue_lock:` goes on holding the old,
now-invisible object while the next caller acquires the fresh one, and both
proceed into the critical section together. The dashboard fires a rehash on
every Settings save, so the trigger is an operator clicking Save during a
transfer.

The fix was to allocate them in `runtime.py`, which nothing reloads, and have
each module bind the name instead of constructing the object.

WHAT #235 DID NOT DO

It fixed the four that existed. It did not stop a fifth. Adding
`_new_lock = threading.Lock()` to `dcc.py` today reintroduces exactly this
bug, and every lock test in the suite stays green - verified by doing it.

That is the shape this repository has been repeatedly bitten by: a defect
fixed instance-by-instance while the class stays open. The same argument as
`scripts/function_coverage.py`, the daemon-thread check in
tests/test_dispatch_threads_are_daemons.py, and the module-reference scan -
all three assert a property over every site rather than over the sites that
happened to be wrong once.

WHY STATIC

Reaching every lock by execution would mean driving every code path that takes
one, and would still only prove the sites a test happened to reach. Reading
the assignments proves it for all of them, including the one added next month.
The thing a grep cannot do - tell a real `threading.Lock()` call from the words
in a comment, or survive a rename - is what the AST does here.
"""

import ast
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402

LOCK_FACTORIES = {"Lock", "RLock", "Condition", "Semaphore", "BoundedSemaphore"}


def constructs_a_lock(value):
    """True when a lock factory is CALLED anywhere in an assignment's value.

    Not only as the whole right-hand side (#749). The shape this missed was
    `x = globals().get("x") or threading.Lock()` - the call sits inside a
    BoolOp, so a scan that looked only at the top node read it as a plain
    expression. Two of them were in dcc.py, rebuilt by `or` on any reload
    whose globals().get came back empty, and every lock test stayed green.
    Walking the value finds a factory call however it is wrapped: `or`,
    a conditional, an argument.
    """
    for node in ast.walk(value):
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None))
            if name in LOCK_FACTORIES:
                return True
    return False


def module_level_locks():
    """(module, line, name) for every module-level lock object constructed."""
    found = []
    for filename in sorted(os.listdir(REPO_ROOT)):
        if not filename.endswith(".py"):
            continue
        with io.open(os.path.join(REPO_ROOT, filename), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        # tree.body only - a lock built inside a function is a local, is not
        # rebound by a reload, and is none of this test's business.
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if not constructs_a_lock(value):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    found.append((filename[:-3], node.lineno, target.id))
    return found


def reloaded_modules():
    """Everything !rehash re-executes: CORE_MODULES plus commands itself."""
    return set(commands.CORE_MODULES) | {"commands"}


class NoReloadedModuleConstructsItsOwnLock(unittest.TestCase):

    def test_every_lock_lives_where_a_rehash_cannot_rebind_it(self):
        reloaded = reloaded_modules()
        offenders = [f"{module}.py:{line}  {name}"
                     for module, line, name in module_level_locks()
                     if module in reloaded]

        self.assertEqual(
            offenders, [],
            "these are rebound on every !rehash, so a thread already inside "
            "the critical section keeps the old object while the next caller "
            "takes the new one:\n  " + "\n  ".join(offenders) +
            "\n\nAllocate it in runtime.py and bind the name here instead, "
            "the way dcc.queue_lock does.")

    def test_runtime_is_where_they_live(self):
        """The other half: they have to be somewhere, and runtime.py is the
        module nothing reloads (pinned by test_runtime_state.py)."""
        owners = {module for module, _line, _name in module_level_locks()}

        self.assertIn("runtime", owners,
                      "runtime.py owns no locks at all, so either they moved "
                      "somewhere else or the scan has stopped seeing them")

    def test_the_scan_finds_the_locks_that_exist(self):
        """Control. A scan matching nothing would pass the assertion above on
        any tree, forever - which is how the guard this file replaces went
        blind without anyone noticing."""
        found = module_level_locks()

        self.assertGreaterEqual(
            len(found), 5,
            "the daemon holds more module-level locks than this; the scan is "
            "not seeing them")

    def test_it_recognises_a_lock_however_it_is_spelled(self):
        """Control for the matcher, driven against synthetic source rather
        than against whatever the tree happens to contain today."""
        source = ("import threading\n"
                  "from threading import Lock\n"
                  "a = threading.Lock()\n"
                  "b = Lock()\n"
                  "c = threading.RLock()\n"
                  "d: object = threading.Condition()\n"
                  "e = 5\n"
                  "g = globals().get('g') or threading.Lock()\n"
                  "h = globals().get('h') or threading.BoundedSemaphore(2)\n"
                  "i = threading.Lock() if e else None\n"
                  "j = globals().get('j') or {}\n"
                  "def f():\n"
                  "    local = threading.Lock()\n")
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8", dir=REPO_ROOT) as handle:
            handle.write(source)
            path = handle.name
        self.addCleanup(os.remove, path)

        mine = sorted(name for module, _line, name in module_level_locks()
                      if module == os.path.basename(path)[:-3])

        self.assertEqual(mine, ["a", "b", "c", "d", "g", "h", "i"],
                         "a lock spelling is unrecognised, or a function-local "
                         "one is being reported")

    def test_a_lock_added_to_a_reloaded_module_would_be_caught(self):
        """The regression this file exists for, driven end to end: #235 fixed
        four locks and nothing stopped a fifth."""
        reloaded = reloaded_modules()
        self.assertTrue(reloaded, "nothing is reloaded, so nothing is at risk")

        pretend = [("dcc", 17, "_new_lock")]
        offenders = [f"{m}.py:{line}  {n}" for m, line, n in pretend if m in reloaded]

        self.assertTrue(offenders,
                        "a lock added to a reloaded module is not recognised "
                        "as a problem, so this guard would not catch one")



class TheLookupLocksAreBoundFromRuntime(unittest.TestCase):
    """#749: dcc.py's scan semaphore and lookup-memory lock were the two the
    or-form hid. Bound by name now, like list_mod._count_lock."""

    def test_they_are_runtimes_objects(self):
        import dcc
        import runtime
        self.assertIs(dcc._library_scans, runtime.library_scans)
        self.assertIs(dcc._lookup_misses_lock, runtime.lookup_memory_lock)

    def test_the_semaphore_admits_exactly_the_scans_dcc_says(self):
        import dcc
        held = 0
        try:
            while held <= dcc.MAX_CONCURRENT_LIBRARY_SCANS and dcc._library_scans.acquire(blocking=False):
                held += 1
            self.assertEqual(held, dcc.MAX_CONCURRENT_LIBRARY_SCANS)
        finally:
            for _ in range(held):
                dcc._library_scans.release()

if __name__ == "__main__":
    unittest.main()
