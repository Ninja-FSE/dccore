"""Live state survives a !rehash because of where it lives, not a list.

`!rehash` calls importlib.reload() on config.py, which re-executes the module
body - so every `dcc_queue = {}` in that file rebound to a new empty container
and the daemon's accumulated state went with it.

commands.py grew a rescue for that: read the containers out before the reload,
put them back after. It worked, but it was a hand-maintained list of names,
and it fell out of step exactly once already - the cross-bot fetch feature
added two containers without touching the list, so a rehash silently emptied
every fetched bot list and reported zero active fetches while transfers were
still running.

The containers now live in runtime.py, which is not reloaded. config.py binds
the same objects, so the several hundred existing `config.dcc_queue` references
did not change, and a reload simply re-runs those bindings and picks the same
live containers back up.

That leaves exactly one way to break it: rebinding instead of mutating.
`config.dcc_queue = {}` detaches config's name from the object runtime.py still
holds, and the two drift apart silently from then on. It is an easy mistake -
the rehash restore path itself made it twice before this change - so the last
tests here read the source and fail the build on any occurrence.
"""

import ast
import contextlib
import copy
import importlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

CONTAINERS = [name for name, value in vars(runtime).items()
              if isinstance(value, (dict, list)) and not name.startswith("_")]


class ConfigAndRuntimeShareTheSameObjects(DCCoreTestCase):

    def test_every_container_is_the_same_object_on_both_modules(self):
        self.assertTrue(CONTAINERS, "runtime.py defines no containers - the "
                                    "discovery above is wrong, not the code")
        for name in CONTAINERS:
            with self.subTest(container=name):
                self.assertIs(getattr(config, name), getattr(runtime, name),
                              f"config.{name} is a different object from "
                              f"runtime.{name}, so writes through one are "
                              f"invisible to the other")

    def test_a_write_through_config_is_visible_on_runtime(self):
        config.dcc_queue["someuser"] = ["Track.flac"]
        self.assertEqual(runtime.dcc_queue, {"someuser": ["Track.flac"]})

    def test_a_write_through_runtime_is_visible_on_config(self):
        runtime.active_transfers.append({"user": "someuser"})
        self.assertEqual(config.active_transfers, [{"user": "someuser"}])


class ContainersSurviveAReload(DCCoreTestCase):
    """The behaviour the whole change exists for."""

    def _reload_config(self):
        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(config)
        self.addCleanup(self._restore)

    def _restore(self):
        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(config)

    def test_a_queued_user_is_still_queued_after_a_reload(self):
        config.dcc_queue["someuser"] = ["Track.flac"]
        config.active_transfers.append({"user": "someuser"})
        config.banned_users["baduser"] = 12345

        self._reload_config()

        self.assertEqual(config.dcc_queue, {"someuser": ["Track.flac"]},
                         "the reload emptied the sharing queue")
        self.assertEqual(config.active_transfers, [{"user": "someuser"}],
                         "the reload lost the active transfer list, so the bot "
                         "would admit work beyond MAX_DCC_SLOTS")
        self.assertEqual(config.banned_users, {"baduser": 12345},
                         "the reload released every timed ban")

    def test_every_container_survives_and_stays_attached(self):
        """Derived rather than named, so a container added to runtime.py in
        future is covered without anyone editing this test."""
        for name in CONTAINERS:
            container = getattr(config, name)
            if isinstance(container, dict):
                container["probe"] = 1
            else:
                container.append("probe")

        self._reload_config()

        for name in CONTAINERS:
            with self.subTest(container=name):
                after = getattr(config, name)
                self.assertIs(after, getattr(runtime, name),
                              "the reload left config detached from runtime")
                self.assertTrue(after, f"the reload emptied {name}")

    def test_scalars_are_still_reset_by_a_reload(self):
        """Not a regression - a deliberate boundary, asserted so it stays one.

        The binding only works for mutable objects; a bool rebound in config.py
        could never write through to runtime.py. So the flags stay in config.py
        and a reload still clears them, which for rar_inprogress is the
        documented "lock-clearing rehash" escape hatch for a wedged packer.
        """
        config.search_inprogress = True
        config.rar_inprogress = True

        self._reload_config()

        self.assertFalse(config.search_inprogress)
        self.assertFalse(config.rar_inprogress)


class NothingRebindsARuntimeContainer(unittest.TestCase):
    """The one way left to break this, caught in the source.

    tests/ is exempt: a fixture assigning `config.dcc_queue = {...}` is normal,
    and tests/support.py's reset_config() re-attaches the name to runtime's
    object at the start of every test, so a detached fixture cannot leak into
    the next test.
    """

    def _production_modules(self):
        return [f for f in sorted(os.listdir(REPO_ROOT))
                if f.endswith(".py") and f != "configure.py"]

    def test_the_scan_finds_modules_to_check(self):
        """Fixture invariant - an empty file list would pass vacuously."""
        self.assertGreater(len(self._production_modules()), 5)

    def _config_aliases(self, tree):
        """Every local name this file's `import defaults as config`/
        `import defaults as X` statements bind - not scoped per-function,
        since a whole-file "any alias found anywhere counts" is precise
        enough for a lint-style check and does not need real scope
        resolution. `commands.py`'s own `import defaults as _cfg` inside
        handle_rehash_request is exactly why this exists: the plain-"config"
        check alone missed it entirely.

        Matches on the REAL module name, "defaults" (#170's RFC renamed the
        file from config.py) - every production module now does
        `import defaults as config`, so matching the old "config" name here
        would silently never match anything again, defeating this entire
        check without a single test failing to say so.
        """
        aliases = {"config"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "defaults":
                        aliases.add(alias.asname or alias.name)
        return aliases

    def test_no_module_rebinds_a_container(self):
        offenders = []
        for filename in self._production_modules():
            path = os.path.join(REPO_ROOT, filename)
            with io.open(path, encoding="utf-8") as handle:
                source = handle.read()
            tree = ast.parse(source)
            config_aliases = self._config_aliases(tree)
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                for target in targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id in config_aliases
                            and target.attr in CONTAINERS):
                        offenders.append(f"{filename}:{node.lineno} "
                                         f"{target.value.id}.{target.attr}")

                # setattr(config, "dcc_queue", ...) rebinds exactly the same
                # way a literal `config.dcc_queue = ...` does, and an alias
                # (`_cfg = config` via `import config as _cfg`) is invisible
                # to the plain-attribute check above - this is what caught
                # handle_rehash_request()'s own restore loop reverting to
                # setattr() on `_cfg`.
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "setattr"
                        and len(node.args) >= 2
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id in config_aliases
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value in CONTAINERS):
                    offenders.append(f"{filename}:{node.lineno} "
                                     f"setattr({node.args[0].id}, "
                                     f"{node.args[1].value!r}, ...)")

        self.assertEqual(
            offenders, [],
            "these rebind a runtime container instead of mutating it, which "
            "detaches config's name from the object runtime.py holds and lets "
            "the two drift apart silently: " + "; ".join(offenders) +
            ". Use .clear()/.update() for a dict, or [:] = ... for a list.")

    def test_config_does_not_define_its_own_containers(self):
        """A container added to config.py instead of runtime.py would not
        survive a rehash, which is the bug this whole change removes."""
        # ADMIN_HOSTMASKS is an empty list, but it is a SETTING - the operator
        # fills it from admin_config.py and it is SUPPOSED to be re-read on a
        # rehash rather than preserved.
        #
        # CUSTOM_THEME used to join it for the same reason (per-role colour
        # overrides, meant to be re-read on a rehash rather than preserved) -
        # #170's RFC flattened it into six plain CUSTOM_THEME_<ROLE> strings,
        # none of which are ast.Dict/ast.List literals any more, so the scan
        # below no longer even considers them and there is nothing left to
        # allow-list here.
        allowed = {"ADMIN_HOSTMASKS"}

        with io.open(os.path.join(REPO_ROOT, "defaults.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        offenders = []
        for node in tree.body:
            # AnnAssign as well as Assign: a container declared as
            # `dcc_queue: dict = {}` is a different node type, and matching
            # only Assign would let it past unnoticed.
            if isinstance(node, ast.Assign):
                targets, value_node = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets, value_node = [node.target], node.value
            else:
                continue
            if not isinstance(value_node, (ast.Dict, ast.List)):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in allowed:
                    offenders.append(f"{target.id} (line {node.lineno})")

        self.assertEqual(
            offenders, [],
            "config.py defines container(s) of its own: " + ", ".join(offenders) +
            ". A rehash reloads config.py and would empty them. Define them in "
            "runtime.py and bind them here, or add to this test's allowlist "
            "with a reason why losing them on a rehash is correct.")

    def test_runtime_is_not_reloaded_by_a_rehash(self):
        """The whole mechanism rests on this. If runtime lands in the reload
        list, every container is emptied again and the tests above still pass,
        because they never go through !rehash itself.

        This read commands.py's AST for a local named `modules_to_reload` and
        broke the moment that literal became the CORE_MODULES constant - a
        refactor that changed no behaviour. It failed for a change that
        improved the code while a real regression (deleting the reload call
        outright) would have left it green, which is the wrong way round.

        Reading the exported value instead: same property, no dependence on
        what the thing is called or where it is written.
        """
        import commands

        self.assertIn("defaults", commands.CORE_MODULES,
                      "fixture invariant: defaults (formerly config.py, "
                      "#170's RFC) should be reloaded")
        self.assertNotIn(
            "runtime", commands.CORE_MODULES,
            "runtime.py is in the reload list, so a rehash re-executes it and "
            "empties every container - exactly the bug moving them there was "
            "meant to remove")


if __name__ == "__main__":
    unittest.main()
