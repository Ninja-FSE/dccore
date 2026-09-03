"""admin_config.py overrides reach the values that are derived from them.

config.py applies admin_config.py at the very bottom, on purpose: an operator
overrides a tracked default without their deployment showing up as a diff.

That works for a plain setting, because nothing has read it yet. It does NOT
work for a setting COMPUTED from another one further up the file - by the time
the override lands, the derived value has already been built from the tracked
default and nothing recomputes it.

BROADCAST_SEARCH_CHANNEL was exactly that. Its own comment promised it
"defaults to the first entry of CHANNEL", and it did - to the first entry of
the SHIPPED default, not the operator's. So an operator running on their own
channel still had a dashboard broadcast search fire its @find into
#dccore-default, a real public channel they may not even be in:

    operator set CHANNEL      = #dccore-test
    BROADCAST_SEARCH_CHANNEL  = #dccore-default

webserver.start_broadcast_search() prefers BROADCAST_SEARCH_CHANNEL over
CHANNEL, so the stale value is the one that gets used.

The last test here is the general guard: it derives the rule from config.py's
own source rather than naming this one setting, so the NEXT value someone
derives above the override point fails instead of shipping.
"""

import ast
import contextlib
import importlib
import io
import os
import shutil
import sys
import tempfile
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class AdminConfigOverrideTests(DCCoreTestCase):

    def _reload_with_admin_config(self, body):
        """Reload config.py with `body` as the machine's admin_config.py.

        Every test writes one - an empty file when it wants "no overrides" -
        so a developer who has a real admin_config.py on their machine gets
        the same result as CI, which has none. Without that, "no overrides"
        would mean "whatever this particular box happens to be configured
        for", which is not a test.
        """
        tmp = tempfile.mkdtemp(prefix="dccore-localcfg-test-")

        # addCleanup is LIFO, so these run in REVERSE order. The final reload
        # has to happen with the temp directory already off sys.path and the
        # module already purged, or it just re-applies the override it is
        # supposed to be undoing - hence it is registered FIRST.
        self.addCleanup(importlib.reload, config)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(lambda: tmp in sys.path and sys.path.remove(tmp))
        self.addCleanup(sys.modules.pop, "admin_config", None)

        with open(os.path.join(tmp, "admin_config.py"), "w", encoding="utf-8") as handle:
            handle.write(body)

        sys.path.insert(0, tmp)
        sys.modules.pop("admin_config", None)
        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(config)
        return config

    def test_a_channel_override_moves_the_broadcast_channel(self):
        """The defect. Overriding CHANNEL must move the derived value with it."""
        cfg = self._reload_with_admin_config('CHANNEL = "#dccore-test"\n')

        self.assertEqual(cfg.CHANNEL, "#dccore-test")
        self.assertEqual(
            cfg.BROADCAST_SEARCH_CHANNEL, "#dccore-test",
            "the broadcast channel still points at the shipped default's first "
            "channel, so a broadcast search would @find into a channel the "
            "operator never configured")

    def test_the_first_entry_is_taken_when_several_are_overridden(self):
        """CHANNEL is a comma-separated list; only the first is broadcast to,
        deliberately - broadcasting into every joined channel multiplies the
        disruption for one search."""
        cfg = self._reload_with_admin_config(
            'CHANNEL = "#first-one,#second-one,#third-one"\n')

        self.assertEqual(cfg.BROADCAST_SEARCH_CHANNEL, "#first-one")

    def test_an_explicit_broadcast_channel_still_wins(self):
        """Control. Deriving the value must not start overwriting an operator
        who set it on purpose - config.py's own comment offers that choice."""
        cfg = self._reload_with_admin_config(
            'CHANNEL = "#dccore-test"\n'
            'BROADCAST_SEARCH_CHANNEL = "#somewhere-else"\n')

        self.assertEqual(cfg.BROADCAST_SEARCH_CHANNEL, "#somewhere-else")

    def test_with_no_overrides_and_no_channel_there_is_nothing_to_derive(self):
        """Control, updated for #170's RFC: CHANNEL is now one of
        settings_file.REQUIRED and ships blank (None) rather than a real
        tracked default (see CHANNEL's own comment in config.py) - a fresh,
        not-yet-configured install has nothing for BROADCAST_SEARCH_CHANNEL
        to derive FROM, so both stay unset. oserve.startup()'s REQUIRED gate
        is what actually refuses to boot in this state; importing config.py
        itself must not raise (see the `and CHANNEL` guard this test pins)."""
        cfg = self._reload_with_admin_config("# no overrides\n")

        self.assertIsNone(cfg.CHANNEL)
        self.assertIsNone(cfg.BROADCAST_SEARCH_CHANNEL)

    def test_a_blank_broadcast_channel_falls_back_rather_than_staying_blank(self):
        """An operator who sets it to "" gets the derived value, not an empty
        string - webserver.start_broadcast_search() returns 503 "No broadcast
        channel configured" on a blank one, which would be a confusing way to
        learn you typed an empty string."""
        cfg = self._reload_with_admin_config(
            'CHANNEL = "#dccore-test"\n'
            'BROADCAST_SEARCH_CHANNEL = ""\n')

        self.assertEqual(cfg.BROADCAST_SEARCH_CHANNEL, "#dccore-test")

    def test_a_nickname_override_moves_the_list_base_name(self):
        """Found live, running configure.py against a real install: NICKNAME came
        out "DCCoreTest", but the generated list still came out named
        "DCCore-<date>.zip" - #170's RFC predicted exactly this ("LIST_BASE_NAME
        can derive from NICKNAME") but nothing did the derivation until now."""
        cfg = self._reload_with_admin_config('NICKNAME = "DCCoreTest"\n')

        self.assertEqual(cfg.NICKNAME, "DCCoreTest")
        self.assertEqual(
            cfg.LIST_BASE_NAME, "DCCoreTest",
            "the generated list is still named after the shipped default, "
            "not the bot's own configured nickname")

    def test_an_explicit_list_base_name_still_wins(self):
        """Control. Deriving the value must not start overwriting an operator
        who set it on purpose - LIST_BASE_NAME's own comment offers that
        choice, same as BROADCAST_SEARCH_CHANNEL's above."""
        cfg = self._reload_with_admin_config(
            'NICKNAME = "DCCoreTest"\n'
            'LIST_BASE_NAME = "SomeOtherName"\n')

        self.assertEqual(cfg.LIST_BASE_NAME, "SomeOtherName")

    def test_with_no_overrides_and_no_nickname_there_is_nothing_to_derive(self):
        """Control: NICKNAME is settings_file.REQUIRED and ships blank - a
        fresh, not-yet-configured install has nothing for LIST_BASE_NAME to
        derive FROM, so it stays at its own shipped literal. Importing
        config.py itself must not raise (see the `and NICKNAME` guard this
        test pins)."""
        cfg = self._reload_with_admin_config("# no overrides\n")

        self.assertIsNone(cfg.NICKNAME)
        self.assertEqual(cfg.LIST_BASE_NAME, "DCCore")


class NoSettingIsDerivedBeforeOverridesLand(unittest.TestCase):
    """The general rule, read out of config.py's own source.

    This deliberately does not name BROADCAST_SEARCH_CHANNEL. The bug was not
    that one setting was wrong, it was that the file's layout let any derived
    value be computed before overrides arrive - so the check is on the layout,
    and the next one someone adds fails here instead of shipping.
    """

    def _parse(self):
        with io.open(os.path.join(REPO_ROOT, "defaults.py"), encoding="utf-8") as handle:
            return ast.parse(handle.read())

    def _override_lines(self, tree):
        """Every point where config.py's namespace is written from outside.

        Found by SHAPE, not by name. Naming the mechanisms would be another
        hand-maintained list that has to stay in step with config.py - which
        is precisely how this check went wrong. It knew only about
        admin_config.py, so when settings.conf arrived as a second and LATER
        override, the check kept passing while a value derived between the two
        silently ignored it.

        Two shapes cover both mechanisms, and anything else of the same kind:

          * a star-import          `from admin_config import *`
          * a call handed the namespace
                                   `settings_file.apply_to(globals())`
        """
        lines = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "*" for alias in node.names):
                lines.append(node.lineno)
            elif isinstance(node, ast.Call):
                for argument in node.args:
                    if (isinstance(argument, ast.Call)
                            and isinstance(argument.func, ast.Name)
                            and argument.func.id == "globals"):
                        lines.append(node.lineno)
        return sorted(lines)

    def _last_override_line(self, tree):
        """A derived value must sit below the LAST of them, not the first."""
        lines = self._override_lines(tree)
        return lines[-1] if lines else None

    def test_overrides_are_applied_somewhere_in_the_file(self):
        """Fixture invariant: with nothing detected, the test below would pass
        vacuously by having no reference point to compare against."""
        self.assertIsNotNone(
            self._last_override_line(self._parse()),
            "no override mechanism found in config.py - the check below has "
            "no reference point and is silently passing")

    def test_both_override_mechanisms_are_seen(self):
        """The specific regression this replaced.

        There are two mechanisms - admin_config.py and settings.conf - and a
        check that spots only the first passes happily while a value derived
        BETWEEN the two ignores the second entirely. Asserting more than one
        is found means losing sight of a mechanism fails here instead of in
        production.
        """
        lines = self._override_lines(self._parse())
        self.assertGreaterEqual(
            len(lines), 2,
            f"only {len(lines)} override point(s) found in config.py, at "
            f"{lines}. config.py applies both admin_config.py and "
            f"settings.conf; a check that sees only one of them lets a value "
            f"derived between the two silently ignore the other.")

    @staticmethod
    def _assignment_parts(node):
        """(targets, value) for a plain or annotated assignment, else (None, None).

        `CHANNEL = "..."` parses to ast.Assign; `CHANNEL: str = None` parses
        to ast.AnnAssign - a different node type, carrying `.target` rather
        than `.targets`. Matching only Assign would make every annotated
        setting invisible to both scans below, and this check would then
        report green while guarding nothing - the exact failure it was
        rewritten to remove.

        `value` is None for a bare annotation (`NICKNAME: str`), which
        derives nothing and is skipped.
        """
        if isinstance(node, ast.Assign):
            return node.targets, node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return [node.target], node.value
        return None, None

    def derived_above_override(self, tree):
        """Settings computed from another setting above the last override.

        Split out of the test so it can also be run against a synthetic
        source - the tests below check that it actually sees both assignment
        forms, rather than the check quietly finding nothing.
        """
        override_line = self._last_override_line(tree)

        assigned = {}
        for node in tree.body:
            targets, _value = self._assignment_parts(node)
            for target in targets or []:
                if isinstance(target, ast.Name):
                    assigned[target.id] = node.lineno

        # ast.walk, not tree.body. A derived value is very likely to sit
        # inside `if not ALREADY_SET:` rather than at the top level - which is
        # exactly what the real one does - and a scan of module-level
        # statements alone cannot see it. config.py defines no functions or
        # classes, so walking the whole tree reaches settings and nothing else.
        offenders = []
        for node in ast.walk(tree):
            targets, value = self._assignment_parts(node)
            if targets is None or value is None or node.lineno > override_line:
                continue
            sources = sorted({inner.id for inner in ast.walk(value)
                              if isinstance(inner, ast.Name) and inner.id in assigned})
            if not sources:
                continue
            target = targets[0]
            if isinstance(target, ast.Name):
                offenders.append(f"{target.id} (line {node.lineno}, from "
                                 f"{', '.join(sources)})")
        return offenders

    def test_no_derived_setting_is_computed_above_the_override_point(self):
        tree = self._parse()
        override_line = self._last_override_line(tree)
        offenders = self.derived_above_override(tree)

        self.assertEqual(
            offenders, [],
            "these settings are computed from another setting ABOVE line "
            f"{override_line}, the LAST point at which overrides are applied, "
            "so they "
            "capture the tracked default and silently ignore the operator's "
            "override: " + "; ".join(offenders) + ". Move the computation to "
            "the DERIVED VALUES section at the end of config.py.")



    def test_the_scan_sees_a_plain_derived_assignment(self):
        """Control - the form that exists in config.py today."""
        source = textwrap.dedent("""
            CHANNEL = "#a,#b"
            BROADCAST = CHANNEL.split(",")[0]
            from admin_config import *
        """)

        offenders = self.derived_above_override(ast.parse(source))

        self.assertEqual(len(offenders), 1)
        self.assertIn("BROADCAST", offenders[0])

    def test_the_scan_sees_an_annotated_derived_assignment(self):
        """The regression this guards against.

        `BROADCAST: str = CHANNEL.split(...)` is an ast.AnnAssign, and a scan
        matching only ast.Assign finds nothing here - reporting the file
        clean while the derived value silently ignores every override.
        """
        source = textwrap.dedent("""
            CHANNEL: str = "#a,#b"
            BROADCAST: str = CHANNEL.split(",")[0]
            from admin_config import *
        """)

        offenders = self.derived_above_override(ast.parse(source))

        self.assertEqual(len(offenders), 1,
                         "an annotated derived setting was invisible to the scan")
        self.assertIn("BROADCAST", offenders[0])

    def test_a_derived_value_below_the_override_point_is_allowed(self):
        """Control - below the last override is the correct place for one, and
        must not be reported."""
        source = textwrap.dedent("""
            CHANNEL: str = "#a,#b"
            from admin_config import *
            BROADCAST: str = CHANNEL.split(",")[0]
        """)

        self.assertEqual(self.derived_above_override(ast.parse(source)), [])

    def test_a_bare_annotation_derives_nothing(self):
        """`BROADCAST: str` declares a type and computes nothing, so it is not
        a derived value however high in the file it sits."""
        source = textwrap.dedent("""
            CHANNEL: str = "#a"
            BROADCAST: str
            from admin_config import *
        """)

        self.assertEqual(self.derived_above_override(ast.parse(source)), [])


if __name__ == "__main__":
    unittest.main()
