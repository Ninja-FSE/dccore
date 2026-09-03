"""A shipped debug channel is a shared room once the repo is public.

#171 gave DEBUG_CHANNEL the default "#dccore-debug" - sensible while this
project was two operators who knew each other. irc.py joins it automatically on
connect and streams the daemon's internals into it: bans, pack failures,
transfer detail, nicknames. On a public release every adopter's bot would join
the same room, broadcast its own operation into it, and read everybody else's.

So it ships blank. That is not "unconfigured": irc.py already guards the JOIN
with `if debug_chan:` and reports when there is none, and both getattr call
sites already fall back to ''. An operator who wants a debug channel names
their own, which is the only answer that can be right for more than one
install.

The controls here matter as much as the change. Blanking a setting is easy to
do in a way that quietly breaks the feature for the operators who DO set one,
and the JOIN guard is what stops a blank value producing a malformed `JOIN`
with no argument.
"""

import ast
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402


def shipped_default(name):
    """What defaults.py declares, read from source rather than from the live
    module - the live one has already had admin_config.py and settings.conf
    applied over it, so it says what THIS machine is configured to, not what
    the project ships."""
    with io.open(os.path.join(REPO_ROOT, "defaults.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            target, value = node.targets[0].id, node.value
        if target == name:
            return ast.literal_eval(value)
    raise AssertionError(f"defaults.py no longer declares {name}")


def irc_source():
    with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
        return handle.read()


class NothingIsShippedPointingAtAChannelWeOwn(unittest.TestCase):

    def test_the_debug_channel_ships_blank(self):
        self.assertEqual(shipped_default("DEBUG_CHANNEL"), "")

    def test_no_project_channel_is_shipped_as_any_default(self):
        """The general rule, so the next setting to want a channel cannot
        reintroduce this. A shipped channel name is a shared room."""
        with io.open(os.path.join(REPO_ROOT, "defaults.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        offenders = []
        for node in tree.body:
            target = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target, value = node.target.id, node.value
            elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                target, value = node.targets[0].id, node.value
            if target is None:
                continue
            try:
                literal = ast.literal_eval(value)
            except Exception:
                continue
            if isinstance(literal, str) and literal.startswith("#"):
                offenders.append(f"{target} = {literal!r}")

        self.assertEqual(offenders, [],
                         "a channel is shipped as a default, so every install "
                         "joins it: " + ", ".join(offenders))

    def test_the_sample_does_not_fill_one_in_either(self):
        """settings.conf.sample is generated, so this catches a regeneration
        against a machine that has one set."""
        with io.open(os.path.join(REPO_ROOT, "settings.conf.sample"), encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip().lstrip("#").strip()
                if stripped.startswith("DEBUG_CHANNEL"):
                    value = stripped.split("=", 1)[1].strip() if "=" in stripped else ""

                    self.assertEqual(value, "", f"the sample ships {value!r}")
                    return
        self.fail("the sample no longer mentions DEBUG_CHANNEL at all")


class ABlankValueJoinsNothing(unittest.TestCase):
    """The guard that makes blank safe. Without it a blank value sends a bare
    `JOIN` with no channel, which is a malformed line, not a no-op."""

    def test_the_join_is_guarded_by_the_value_being_present(self):
        body = irc_source()
        start = body.index("debug_chan = str(getattr(config, 'DEBUG_CHANNEL'")
        window = body[start:start + 500]

        self.assertIn("if debug_chan:", window)
        self.assertLess(window.index("if debug_chan:"), window.index("JOIN {debug_chan}"),
                        "the JOIN is built before the value is checked")

    def test_the_operator_is_told_none_was_joined(self):
        """Silently not joining is indistinguishable from joining and being
        refused, and now it is the default state rather than a rare one."""
        self.assertIn("No DEBUG_CHANNEL is set", irc_source())

    def test_both_call_sites_already_default_to_empty(self):
        """A getattr fallback naming a real channel would reintroduce the
        shared room for any install whose config omits the setting."""
        offenders = []
        for name in ("irc.py", "commands.py"):
            with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "getattr" and len(node.args) == 3):
                    continue
                target, attr, default = node.args
                if not (isinstance(attr, ast.Constant) and attr.value == "DEBUG_CHANNEL"):
                    continue
                try:
                    literal = ast.literal_eval(default)
                except Exception:
                    continue
                if literal:
                    offenders.append(f"{name}:{node.lineno} -> {literal!r}")

        self.assertEqual(offenders, [], "; ".join(offenders))


class AnOperatorWhoWantsOneStillGetsIt(unittest.TestCase):
    """The control. Blanking the default must not disable the feature."""

    def test_a_configured_channel_is_still_joined(self):
        """Requires a LIVE send, not merely the text "JOIN {debug_chan}"
        somewhere in the window. The first version of this test asserted the
        substring, and passed against `pass  # socket_conn.send(...)` - which
        is the whole feature commented out. Comment lines are skipped here for
        exactly that reason."""
        body = irc_source()
        start = body.index("debug_chan = str(getattr(config, 'DEBUG_CHANNEL'")
        window = body[start:start + 500]

        # Parsed, not grepped. A line-based check has to guess where the code
        # ends and a comment begins, and the second version of this test still
        # passed against `pass  # socket_conn.send(f"JOIN {debug_chan}")`
        # because the comment sits mid-line. The AST simply does not contain
        # commented-out code.
        tree = ast.parse(body)
        live = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            segment = ast.get_source_segment(body, node) or ""
            if "JOIN {debug_chan}" in segment and ".send(" in segment:
                live.append(node.lineno)

        self.assertTrue(live, "nothing actually sends a JOIN for a configured "
                              "debug channel; the call is missing or commented out")

    def test_the_debug_sinks_are_still_wired(self):
        """DEBUG_TO_CHANNEL is what actually routes output there; blanking the
        channel must not have been mistaken for turning the feature off."""
        self.assertTrue(hasattr(config, "DEBUG_TO_CHANNEL"))
        self.assertEqual(shipped_default("DEBUG_TO_CHANNEL"), True)


if __name__ == "__main__":
    unittest.main()
