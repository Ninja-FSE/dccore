"""!rehash re-reads admin_config.py, so revoking access actually revokes it.

THE MECHANISM, WHICH IS THE WHOLE FINDING

defaults.py ends with `from admin_config import *`. Reloading defaults
re-executes that statement - but `from X import *` asks the import system for
X, and the import system answers out of sys.modules. It does not go back to
the file. So reloading defaults alone rebinds defaults' names to the values the
CACHED admin_config module already had, which are the values from process
start.

The operator's experience: delete a hostmask from ADMIN_HOSTMASKS, run
!rehash, read "Rehash completed!", and the revoked admin still has every admin
command until the process is restarted. Nothing reports a failure, because
nothing failed - the reload did exactly what it was asked to do.

Revocation is the direction that matters. A newly ADDED admin who does not work
yet is a puzzle the operator will chase; an admin who was removed and still
works is one nobody checks.

WHY THE TESTS BUILD THEIR OWN MODULE PAIR

Driving the real admin_config.py would mean writing a file into the repository
root that is gitignored precisely so it never exists in a checkout, and
reloading the real defaults mid-suite would reset module-level state every other
test depends on. The pair below has the same shape - a source module, and a
consumer doing `from source import *` - so it exercises the same import-system
behaviour without touching the daemon's own modules.

The last test is the one that ties the mechanism to the daemon: it asserts the
real CORE_MODULES puts admin_config before defaults, which is the property the
first two prove is load-bearing.
"""

import importlib
import io
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class ModulePairCase(unittest.TestCase):
    """A source module and a consumer that does `from source import *`."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dccore-reload-")
        self.addCleanup(self._cleanup)
        sys.path.insert(0, self.dir)

        self.source = "fake_admin_config"
        self.consumer = "fake_defaults"
        self.write_source(["allowed"])
        self.write(self.consumer,
                   f"from {self.source} import *  # noqa: F401,F403\n")

        importlib.import_module(self.source)
        importlib.import_module(self.consumer)

    def _cleanup(self):
        for name in (self.source, self.consumer):
            sys.modules.pop(name, None)
        if self.dir in sys.path:
            sys.path.remove(self.dir)
        for name in os.listdir(self.dir):
            try:
                os.remove(os.path.join(self.dir, name))
            except OSError:
                pass

    def write(self, module, text):
        path = os.path.join(self.dir, module + ".py")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        # A .pyc newer than the source would be served instead of the edit, and
        # two writes inside one filesystem timestamp tick is exactly this test.
        importlib.invalidate_caches()

    def write_source(self, hostmasks):
        self.write(self.source, f"ADMIN_HOSTMASKS = {hostmasks!r}\n")

    def hostmasks(self):
        return getattr(sys.modules[self.consumer], "ADMIN_HOSTMASKS")


class ReloadingTheConsumerAloneIsNotEnough(ModulePairCase):

    def test_the_edit_is_invisible(self):
        """The defect, reproduced. This is what !rehash used to do."""
        self.write_source([])  # the operator revokes the only admin

        commands.reload_modules_in_order(modules=(self.consumer,),
                                         reload_self=False)

        self.assertEqual(self.hostmasks(), ["allowed"],
                         "the cached module was re-read after all - if this "
                         "fails, Python's import semantics changed and the "
                         "ordering below is no longer load-bearing")


class ReloadingTheSourceFirstApplies(ModulePairCase):

    def test_a_revoked_entry_is_gone(self):
        self.write_source([])

        commands.reload_modules_in_order(
            modules=(self.source, self.consumer), reload_self=False)

        self.assertEqual(self.hostmasks(), [])

    def test_an_added_entry_arrives(self):
        self.write_source(["allowed", "second-operator"])

        commands.reload_modules_in_order(
            modules=(self.source, self.consumer), reload_self=False)

        self.assertEqual(self.hostmasks(), ["allowed", "second-operator"])

    def test_the_wrong_order_does_not_work(self):
        """Control for the ordering itself, not just for reloading both.
        Consumer first, then source: the consumer still copied the stale values
        before the source was refreshed."""
        self.write_source([])

        commands.reload_modules_in_order(
            modules=(self.consumer, self.source), reload_self=False)

        self.assertEqual(self.hostmasks(), ["allowed"])

    def test_a_module_that_is_not_loaded_is_skipped(self):
        """admin_config.py is optional and gitignored, so absent is the normal
        case, not an error."""
        reloaded = commands.reload_modules_in_order(
            modules=("no_such_module_at_all", self.source), reload_self=False)

        self.assertEqual(reloaded, [self.source])


class TheDaemonsOwnOrderIsRight(unittest.TestCase):
    """What the two classes above prove matters, checked against the real list."""

    def test_admin_config_is_reloaded(self):
        self.assertIn("admin_config", commands.CORE_MODULES)

    def test_it_is_reloaded_before_defaults(self):
        order = list(commands.CORE_MODULES)

        self.assertLess(order.index("admin_config"), order.index("defaults"),
                        "defaults would copy the stale admin_config values")

    def test_every_module_it_names_exists(self):
        """A typo here fails silently: the name is skipped by the `in
        sys.modules` guard and that module simply never reloads."""
        for name in commands.CORE_MODULES:
            if name == "admin_config":
                continue  # optional and gitignored; absent in a clean checkout
            with self.subTest(module=name):
                self.assertTrue(
                    os.path.exists(os.path.join(REPO_ROOT, name + ".py")),
                    f"{name} is in CORE_MODULES but there is no {name}.py")

    def test_commands_reloads_itself_last(self):
        """Reloading this module rebinds the names the caller is running out
        of. Doing it first would be a different bug."""
        reloaded = commands.reload_modules_in_order(modules=(), reload_self=True)

        self.assertEqual(reloaded, ["commands"])


class TheHandlerActuallyReloads(DCCoreTestCase):
    """handle_rehash_request() is one of the twenty-one functions the audit
    found with no behavioural coverage, and a mutation run made the cost
    concrete: deleting the reload call from the handler entirely left every
    test above green. !rehash would back up the live state, restore it, and
    announce "Rehash completed!" without reloading a single module.

    The reload step is replaced with a recorder rather than allowed to run:
    reloading the real defaults/dcc/announce in the middle of a test run resets
    module-level state the rest of the suite is built on. What is being pinned
    here is that the handler CALLS it - the call's behaviour is pinned by the
    classes above.
    """

    def setUp(self):
        super().setUp()
        self.calls = []
        self.real = commands.reload_modules_in_order
        commands.reload_modules_in_order = self.record
        self.addCleanup(setattr, commands, "reload_modules_in_order", self.real)

    def record(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return []

    def test_an_authorised_rehash_reloads(self):
        commands.handle_rehash_request("operator", "#channel", authorised=True)

        self.assertEqual(len(self.calls), 1,
                         "!rehash completed without reloading anything")

    def test_an_unauthorised_rehash_reloads_nothing(self):
        """The other half. A rehash from a stranger must not reach the reload,
        and the security check is what keeps it from doing so."""
        self.set_config(ADMIN_NICK="operator")

        commands.handle_rehash_request("stranger", "#channel")

        self.assertEqual(self.calls, [])

    def test_the_admin_nick_still_gets_through_unauthorised_flag(self):
        """Control for the check above: it refuses strangers, not everyone."""
        self.set_config(ADMIN_NICK="operator")

        commands.handle_rehash_request("operator", "#channel")

        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
