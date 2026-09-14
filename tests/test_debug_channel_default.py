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

    def test_a_blank_value_puts_nothing_in_the_join(self):
        """Driven now rather than read (#510). The guard used to be an
        `if debug_chan:` wrapped around a JOIN of its own, and this test
        asserted that shape - but the debug channel rides in the ordinary
        batching since the connect path stopped sending it as a lone trailing
        command that the server was dropping. Same property, different place
        to ask it.

        A bare `JOIN` with no channel is a malformed line, not a no-op, which
        is why blank has to be dropped rather than passed through.
        """
        import irc

        config.CHANNEL = "#alpha,#beta"
        config.DEBUG_CHANNEL = ""

        wanted = irc.channels_we_should_be_in()

        self.assertEqual(wanted, ["#alpha", "#beta"])
        for payload in irc.join_batches(wanted):
            with self.subTest(payload=payload):
                self.assertTrue(all(part for part in payload.split(",")), payload)

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


class ABlankChannelDropsNoLineEither(unittest.TestCase):
    """#424: the JOIN guard above stops a blank DEBUG_CHANNEL from producing
    a malformed JOIN. announce.send_debug() had no equivalent guard at all -
    on the shipped defaults (DEBUG_CHANNEL="", DEBUG_TO_CHANNEL=True), it
    queued "PRIVMSG  :<...>" - two spaces, no recipient - which the server
    silently rejects, and counted it as delivered so the stdout floor never
    fired. Every send_debug-only outcome (several in commands.py have no
    print() beside them) was invisible on IRC, on the console and on stdout
    alike, on a stock install.
    """

    def setUp(self):
        import announce

        self.announce = announce
        self.real_channel = getattr(config, "DEBUG_CHANNEL", "")
        self.real_to_channel = getattr(config, "DEBUG_TO_CHANNEL", True)
        self.real_to_console = getattr(config, "DEBUG_TO_CONSOLE", True)
        self.addCleanup(setattr, config, "DEBUG_CHANNEL", self.real_channel)
        self.addCleanup(setattr, config, "DEBUG_TO_CHANNEL", self.real_to_channel)

        # THE DRAIN THREAD IS NEVER STOPPED ONCE STARTED, and these assertions
        # are about what gets ENQUEUED rather than about delivery. Any earlier
        # test in the run that logged to a configured channel leaves the real
        # drain running, and it then empties this queue before the assertion
        # below can look at it - so the test passes alone and fails in a full
        # run, which is the worst way for it to fail.
        #
        # Closing the gate is what test_debug_routing.py already does for the
        # same reason.
        self._drain_started = self.announce._debug_drain_started
        self.announce._debug_drain_started = True
        self.addCleanup(setattr, self.announce, "_debug_drain_started",
                        self._drain_started)
        self.addCleanup(setattr, config, "DEBUG_TO_CONSOLE", self.real_to_console)

        # A thread left over from an earlier test could otherwise drain
        # whatever this test queues before the assertion runs.
        announce._debug_queue.clear()
        self.addCleanup(announce._debug_queue.clear)

    def test_a_blank_channel_is_not_queued_for_the_server(self):
        config.DEBUG_CHANNEL = ""
        config.DEBUG_TO_CHANNEL = True
        config.DEBUG_TO_CONSOLE = False

        self.announce.send_debug("Admin SysOp added a ban", category="BAN")

        self.assertEqual(list(self.announce._debug_queue), [],
                         "a recipient-less PRIVMSG was queued for a channel "
                         "that does not exist")

    def test_a_blank_channel_falls_through_to_stdout(self):
        """The floor this whole feature rests on: nothing may vanish."""
        config.DEBUG_CHANNEL = ""
        config.DEBUG_TO_CHANNEL = True
        config.DEBUG_TO_CONSOLE = False

        buf = io.StringIO()
        real_stdout = sys.stdout
        sys.stdout = buf
        try:
            self.announce.send_debug("Admin SysOp added a ban", category="BAN")
        finally:
            sys.stdout = real_stdout

        self.assertIn("Admin SysOp added a ban", buf.getvalue())

    def test_a_configured_channel_is_still_queued(self):
        """Control: an operator who DOES set one must not lose the feature -
        the same reason AnOperatorWhoWantsOneStillGetsIt exists for the JOIN
        side of this."""
        config.DEBUG_CHANNEL = "#chan"
        config.DEBUG_TO_CHANNEL = True
        config.DEBUG_TO_CONSOLE = False

        self.announce.send_debug("Admin SysOp added a ban", category="BAN")

        self.assertEqual(len(self.announce._debug_queue), 1)


class AnOperatorWhoWantsOneStillGetsIt(unittest.TestCase):
    """The control. Blanking the default must not disable the feature."""

    def test_a_configured_channel_is_still_joined(self):
        """DRIVEN, not read (#510).

        Three earlier versions of this test read the source. The first
        asserted a substring and passed against `pass  # socket_conn.send(...)`
        - the whole feature commented out. The second skipped comment lines
        and still passed, because the comment sat mid-line. The third parsed
        the AST and was genuinely correct, right up until the debug channel
        stopped being a JOIN statement of its own - at which point it was
        asserting the shape of the old fix rather than the behaviour. (A
        fourth correction crossed with this one in #504, narrowing that AST
        check from ".send(" to ".send" so it could not pass merely because
        "sendall(" contains "send" - right, and superseded by dropping the
        source read altogether.)

        The behaviour is: a configured debug channel is one of the channels
        the connect path asks for. That is a function now, so ask it - and
        check separately that the connect path is still built from it, so this
        cannot pass against a function nothing calls.
        """
        import irc

        config.CHANNEL = "#alpha,#beta"
        config.DEBUG_CHANNEL = "#thedebug"

        asked_for = []
        for payload in irc.join_batches(irc.channels_we_should_be_in()):
            asked_for.extend(payload.split(","))

        self.assertIn("#thedebug", asked_for,
                      "a configured debug channel is not in the JOIN at all")
        self.assertIn("join_batches(", irc_source(),
                      "the connect path no longer sends what join_batches() "
                      "builds, so the check above proves nothing about it")

    def test_the_debug_sinks_are_still_wired(self):
        """DEBUG_TO_CHANNEL is what actually routes output there; blanking the
        channel must not have been mistaken for turning the feature off."""
        self.assertTrue(hasattr(config, "DEBUG_TO_CHANNEL"))
        self.assertEqual(shipped_default("DEBUG_TO_CHANNEL"), True)


if __name__ == "__main__":
    unittest.main()
