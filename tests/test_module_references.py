"""A module named as a STRING must be a module that exists.

WHY THIS IS ITS OWN CHECK

Several places refer to a module by name rather than by import:

    modules_to_reload = ['config', 'list', 'dcc', 'announce', ...]
    for mod_name in ['dcc', 'config', 'oserve']:
        mod = sys.modules.get(mod_name)
    rar_command(getattr(sys.modules.get("config"), "RAR_BINARY", None))

None of those is checked by anything. The name is a string, so a renamed or
deleted module leaves it pointing at nothing, and **every one of these fails
silently**:

  * `!rehash` iterates `modules_to_reload` and reloads what it finds. A name
    that no longer resolves is skipped, so the module is never reloaded - and
    since config.py's body is what re-reads settings.conf, a rehash would stop
    picking up settings changes while still reporting success.
  * The `sys.modules.get(name)` loops are all guarded by `if mod:`, so a
    missing module is skipped without a word.
  * `platform_compat.py`'s lookup degrades to `getattr(None, "RAR_BINARY",
    None)` -> None -> the rar binary search quietly falls back to PATH.

WHAT PROMPTED IT

Renaming config.py was proposed in issue #100. The mechanical part - 37
imports and ~1400 `config.X` references - is easy and loud. The string
references are neither: a search-and-replace of `config.` never touches
`'config'`, and nothing would have failed.

The two existing tests that mention `modules_to_reload` cannot catch it
either, because both assert MEMBERSHIP - that `config` is in the list, that
`runtime` and `adminchat` are not. A stale name is still a member, so nothing
in those assertions is capable of noticing that it no longer resolves.

This check is deliberately about existence rather than membership. It does not
claim to be the only thing that would break after a rename - a partly-finished
rename breaks loudly in a dozen ways - only that the string references are the
part nothing was watching.
"""

import ast
import io
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def module_name_literals(source):
    """Every string literal in `source` that is used as a module name.

    Three shapes, which is all the codebase uses:

        for mod_name in ['dcc', 'config']:      a loop over module names
        modules_to_reload = ['config', ...]     the !rehash list
        sys.modules.get("config")               a direct lookup

    Returns [(module_name, lineno), ...].
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.For) and isinstance(node.iter, ast.List)
                and isinstance(node.target, ast.Name)
                and "mod" in node.target.id.lower()):
            for element in node.iter.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    found.append((element.value, node.lineno))

        # AnnAssign as well as Assign: `modules_to_reload: list = [...]` is a
        # different node type, and matching only ast.Assign would drop the one
        # list this scan most exists to check.
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            node_targets = (node.targets if isinstance(node, ast.Assign)
                            else [node.target])
            for target in node_targets:
                # Matched on SHAPE, not on one hard-coded variable name, and
                # on a tuple as well as a list. This used to require the name
                # `modules_to_reload` and an ast.List. #199 renamed that local
                # to the constant CORE_MODULES and made it a tuple - and this
                # scan silently stopped seeing four of the modules it names
                # (announce, security, db, stats_mgr), which appear nowhere
                # else in commands.py.
                #
                # Nothing failed, because the OTHER shape below - the
                # sys.modules.get() calls - kept finding enough names in
                # commands.py to satisfy the test that was supposed to notice.
                # A guard that goes blind and stays green is the failure this
                # whole file exists to prevent, so it now matches any
                # uppercase-or-reload-named sequence of string literals.
                name = target.id if isinstance(target, ast.Name) else ""
                looks_like_a_module_list = (
                    "modules" in name.lower() or "reload" in name.lower())
                if (looks_like_a_module_list
                        and isinstance(node.value, (ast.List, ast.Tuple))):
                    for element in node.value.elts:
                        if isinstance(element, ast.Constant) and isinstance(element.value, str):
                            found.append((element.value, node.lineno))

        if isinstance(node, ast.Call):
            function = node.func
            if (isinstance(function, ast.Attribute) and function.attr == "get"
                    and isinstance(function.value, ast.Attribute)
                    and function.value.attr == "modules"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                found.append((node.args[0].value, node.lineno))
    return found


class EveryNamedModuleExists(unittest.TestCase):

    def _references(self):
        """{module_name: ["file:line", ...]} across the daemon's own source."""
        references = {}
        for filename in sorted(os.listdir(REPO_ROOT)):
            if not filename.endswith(".py"):
                continue
            with io.open(os.path.join(REPO_ROOT, filename), encoding="utf-8") as handle:
                source = handle.read()
            for name, lineno in module_name_literals(source):
                references.setdefault(name, []).append(f"{filename}:{lineno}")
        return references

    def test_every_module_referred_to_by_name_exists(self):
        references = self._references()

        # admin_config.py is gitignored: it holds the operator's own overrides
        # and is absent in every clean checkout by design. The reload list
        # names it on purpose, and commands.reload_modules_in_order() skips
        # what is not in sys.modules. Surfaced the moment the scan above
        # regained sight of CORE_MODULES, which is the guard working.
        ABSENT_BY_DESIGN = {"admin_config"}

        missing = []
        for name, sites in sorted(references.items()):
            if name in ABSENT_BY_DESIGN:
                continue
            if not os.path.exists(os.path.join(REPO_ROOT, f"{name}.py")):
                missing.append(f"{name!r} (named at {', '.join(sites)})")

        self.assertEqual(
            missing, [],
            "these names refer to modules that do not exist: " +
            "; ".join(missing) + ". Every one fails silently - a rehash skips "
            "the module, a sys.modules lookup returns None past an `if mod:` "
            "guard. Rename the string alongside the file.")

    def test_the_scan_finds_something_to_check(self):
        """Fixture invariant. If the scan stops matching - because the code
        adopts a shape it does not know - this file would pass while checking
        nothing, which is the failure it exists to prevent."""
        references = self._references()

        self.assertGreaterEqual(
            len(references), 5,
            f"only {len(references)} module name(s) found across the source. "
            f"The scan probably stopped recognising a shape it used to match, "
            f"rather than the code having stopped using module names.")

    def test_the_rehash_reload_list_is_among_them(self):
        """The most important single list, asserted BY ITS CONTENTS.

        This used to assert only that some module name was found somewhere in
        commands.py - which stayed true after #199 renamed the list, because
        commands.py is full of unrelated sys.modules.get() calls. The scan had
        gone blind to four of the eight modules the rehash reloads and this
        test reported everything fine.

        Naming the members is the point: the failure mode is the scan quietly
        matching a different shape, and only the contents can tell.
        """
        import commands

        references = self._references()
        for module in commands.CORE_MODULES:
            if module == "admin_config":
                continue  # optional, gitignored, absent in a clean checkout
            with self.subTest(module=module):
                self.assertIn(
                    module, references,
                    f"{module} is reloaded by !rehash but the scan does not "
                    f"see it named anywhere - if it is ever renamed, nothing "
                    f"will notice the reload list still holds the old name")


class TheScanSeesEachShape(unittest.TestCase):
    """The scan is the thing that could silently stop working, so each shape
    it claims to recognise is pinned against a synthetic source."""

    def test_a_loop_over_module_names(self):
        source = "for mod_name in ['dcc', 'config']:\n    pass\n"
        self.assertEqual(
            [name for name, _ in module_name_literals(source)], ["dcc", "config"])

    def test_the_rehash_list(self):
        source = "modules_to_reload = ['config', 'list', 'dcc']\n"
        self.assertEqual(
            [name for name, _ in module_name_literals(source)],
            ["config", "list", "dcc"])

    def test_a_direct_sys_modules_lookup(self):
        source = 'mod = sys.modules.get("oserve")\n'
        self.assertEqual([name for name, _ in module_name_literals(source)], ["oserve"])

    def test_an_unrelated_list_of_strings_is_not_mistaken_for_module_names(self):
        """Control. Only lists bound to a module-ish loop variable or to
        modules_to_reload count - an ordinary list of strings must not be
        dragged in and reported as a missing module."""
        source = "for channel in ['#one', '#two']:\n    pass\n"
        self.assertEqual(module_name_literals(source), [])

    def test_a_non_literal_lookup_is_ignored(self):
        """sys.modules.get(some_variable) cannot be checked statically, and
        must not raise or produce a phantom name."""
        source = "mod = sys.modules.get(whatever)\n"
        self.assertEqual(module_name_literals(source), [])


if __name__ == "__main__":
    unittest.main()
