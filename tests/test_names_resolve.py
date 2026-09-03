"""Names a function reads that nothing ever defines.

WHY THIS FILE EXISTS

The advert worker spent a release calling a variable that had been deleted:

    [CRITICAL ANNOUNCE ERROR] The thread hit an error:
        name 'speed_bytes_per_sec' is not defined

The speed sampling moved out of announce.py into stats_mgr.live_speed(), the
local that held the figure went with it, and one line further down still named
it - the one that builds the CTCP SLOTS payload. So the human-readable advert
went out every cycle and the machine-readable half never did, for as long as
the daemon ran.

NOTHING CAUGHT IT, AND NOTHING WAS GOING TO

  * Python resolves an unknown bare name at RUNTIME. The module imports, the
    file compiles, py_compile and compileall are both happy.
  * The failure is inside a `while True` in a daemon thread, wrapped in
    try/except so one bad cycle does not kill the advert forever. It printed
    and carried on.
  * 1252 tests passed. None of them runs announce_worker() - it is a loop with
    a sleep in it, so it had been left alone.

The bug is not interesting. The hole is: any function in this codebase can
reference a name that does not exist, and everything short of running that
exact line will pass.

WHAT THIS CHECKS

For every function in every module: a name it READS, which Python will look up
as a global, and which the module never defines and is not a builtin. That is
precisely "somebody deleted the assignment and left the use", and it is decided
without executing anything.

It is deliberately narrow. It says nothing about types, values, or whether a
function is correct - only that every name it mentions can be found.
"""

import builtins
import io
import os
import symtable
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Names the interpreter puts in every module without them appearing in the
# source, so symtable does not list them among the module's own symbols.
IMPLICIT_MODULE_NAMES = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__path__", "__annotations__", "__debug__",
}


def daemon_modules():
    """Every .py the daemon is built from. admin_config.py is the operator's."""
    return sorted(name for name in os.listdir(REPO_ROOT)
                  if name.endswith(".py") and name != "admin_config.py")


def unresolvable_names(source, label):
    """[(where, name)] for every global read in a function that nothing defines.

    A bare name a function never assigns is compiled as a global load, so the
    question is only whether the module - or the builtins - actually has one.
    """
    top = symtable.symtable(source, label, "exec")
    known = ({symbol.get_name() for symbol in top.get_symbols()}
             | set(dir(builtins)) | IMPLICIT_MODULE_NAMES)

    found = []

    def walk(scope, trail):
        for child in scope.get_children():
            if child.get_type() == "function":
                for symbol in child.get_symbols():
                    # is_global: the compiler resolved it as a global load.
                    # not is_assigned: this scope never binds it, so there is
                    # no local for it either.
                    if (symbol.is_global() and symbol.is_referenced()
                            and not symbol.is_assigned()
                            and symbol.get_name() not in known):
                        found.append((".".join(trail + [child.get_name()]),
                                      symbol.get_name()))
            walk(child, trail + [child.get_name()])

    walk(top, [])
    return found


class EveryNameAFunctionReadsExists(unittest.TestCase):

    def test_no_module_reads_a_name_nothing_defines(self):
        offenders = []
        for name in daemon_modules():
            with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                source = handle.read()
            for where, missing in unresolvable_names(source, name):
                offenders.append("%s: %s reads %r, which nothing defines"
                                 % (name, where, missing))

        self.assertEqual(
            offenders, [],
            "a function reads a name that does not exist. Python only finds "
            "this when that line runs, which for a background worker can be "
            "long after release:\n  " + "\n  ".join(offenders))

    def test_the_check_catches_the_bug_it_was_written_for(self):
        """The control, and not a hypothetical one: this is the edit that
        shipped. Without it the test above passes on any codebase, including
        one where every function is broken."""
        source = (
            "import stats_mgr\n"
            "def announce_worker():\n"
            "    speed_str = stats_mgr.format_speed(stats_mgr.live_speed())\n"
            "    payload = f'SLOTS {int(speed_bytes_per_sec)}'\n"
            "    return speed_str, payload\n"
        )

        found = unresolvable_names(source, "announce.py")

        self.assertEqual(found, [("announce_worker", "speed_bytes_per_sec")])

    def test_a_name_the_function_does_assign_is_not_reported(self):
        """The fix, as a control in the other direction - so the check cannot
        pass by reporting everything."""
        source = (
            "import stats_mgr\n"
            "def announce_worker():\n"
            "    speed_bytes_per_sec = stats_mgr.live_speed()\n"
            "    return f'SLOTS {int(speed_bytes_per_sec)}'\n"
        )

        self.assertEqual(unresolvable_names(source, "announce.py"), [])

    def test_module_level_names_are_not_reported(self):
        """A function reading a module global is the normal case, and by far
        the most common shape in this codebase - config, oserve, the lot."""
        source = (
            "import config\n"
            "LIMIT = 5\n"
            "def check():\n"
            "    return config.NICKNAME, LIMIT\n"
        )

        self.assertEqual(unresolvable_names(source, "x.py"), [])

    def test_builtins_and_implicit_module_names_are_not_reported(self):
        """__file__ is never written in the source and every module has one;
        an earlier version of this reported it in commands.py."""
        source = (
            "import os\n"
            "def where():\n"
            "    return os.path.dirname(os.path.abspath(__file__)), len('ab')\n"
        )

        self.assertEqual(unresolvable_names(source, "x.py"), [])

    def test_a_nested_function_is_checked_too(self):
        """commands.py builds its list updater as a closure, and dcc.py does
        the same for its senders. A checker that only looked at top-level
        functions would miss most of the interesting code."""
        source = (
            "def outer():\n"
            "    def inner():\n"
            "        return missing_name\n"
            "    return inner\n"
        )

        self.assertEqual(unresolvable_names(source, "x.py"),
                         [("outer.inner", "missing_name")])

    def test_a_closure_variable_is_not_reported(self):
        """A name the inner function reads from the enclosing one is a free
        variable, not a global - reporting it would make every closure in the
        codebase a false alarm."""
        source = (
            "def outer():\n"
            "    value = 1\n"
            "    def inner():\n"
            "        return value\n"
            "    return inner\n"
        )

        self.assertEqual(unresolvable_names(source, "x.py"), [])

    def test_it_looks_at_every_module_not_a_handful(self):
        """The floor that stops the scan quietly shrinking to nothing - the
        same reason scripts/preflight.py pins a minimum test count."""
        modules = daemon_modules()

        self.assertGreaterEqual(len(modules), 15,
                                "the module scan is only seeing %d files: %s"
                                % (len(modules), modules))
        self.assertIn("announce.py", modules)


if __name__ == "__main__":
    unittest.main()
