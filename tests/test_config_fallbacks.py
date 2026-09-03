"""getattr(config, 'X', <default>) is a second declaration of X.

There are 68 of them across the daemon. Each is a small claim about what X
should be when config.py does not say - and config.py always does say, so
almost none of them can ever fire. That is exactly what makes them dangerous:
a value nothing exercises is a value nothing corrects, and several had already
drifted into contradicting the file they were standing in for.

  DEBUG_CHANNEL    config.py says "#example-serv"
                   commands.py said "#example-debug"   <- a channel that is not it
                   irc.py said "#example-serv"

  ADMIN_NICK       config.py says "SysOp,Op2"
                   commands.py said "SysOp"          <- one operator, not two

  PAUSE_ON_UPDATE  config.py says True
                   three modules said False         <- the exact inversion

The rule needs no list of names to keep up to date: a fallback may not disagree
with the declared default. If config.py is the source of truth then a literal
that differs from it is wrong by construction, whichever of the two was meant.

WHY THE ADMIN ONE IS NOT MERELY UNTIDY

is_admin()'s docstring records removing `or user.lower() == "sysop"` because it
"made the literal nick sysop an admin regardless of what config.ADMIN_NICK was
set to - an undocumented second account nobody could turn off". Four lines
below that paragraph, the default was 'SysOp'. The same account through a
different door, and an Undernet nick is not owned without services auth.
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
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

# Names the daemon assigns to config at RUNTIME rather than reads from it, so
# config.py has no declaration to compare against. ORIGINAL_NICK is set by
# irc.py after a 433 fallback; MY_IP_OR_DOCK is detected at startup.
RUNTIME_ASSIGNED = {"ORIGINAL_NICK", "MY_IP_OR_DOCK", "fetch_feature_disabled"}


def modules():
    for name in sorted(os.listdir(REPO_ROOT)):
        if name.endswith(".py") and name != "admin_config.py":
            yield name, os.path.join(REPO_ROOT, name)
    scripts = os.path.join(REPO_ROOT, "scripts")
    for name in sorted(os.listdir(scripts)):
        if name.endswith(".py"):
            yield "scripts/" + name, os.path.join(scripts, name)


def declared_defaults():
    """Every setting config.py declares, and the value it declares."""
    with io.open(os.path.join(REPO_ROOT, "defaults.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            target, value = node.targets[0].id, node.value
        else:
            continue
        try:
            out[target] = ast.literal_eval(value)
        except Exception:
            continue
    return out


def fallbacks():
    """Every getattr(config, 'NAME', <literal>) in the daemon."""
    found = []
    for label, path in modules():
        with io.open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr" and len(node.args) == 3):
                continue
            obj, name, default = node.args
            if not (isinstance(obj, ast.Name) and obj.id == "config"):
                continue
            if not isinstance(name, ast.Constant):
                continue
            try:
                value = ast.literal_eval(default)
            except Exception:
                continue
            found.append((label, node.lineno, name.value, value))
    return found


def contradictions(declared, found):
    """The fallbacks that disagree with what config.py declares.

    A falsy fallback is allowed: it is not a second opinion about the value, it
    is a decline to have one. adminchat reporting 0 slots when it cannot read
    the setting is the conservative answer, not a claim that the bot has none.

    That allowance is also this rule's one blind spot, and PAUSE_ON_UPDATE is
    why it is worth naming: False passed here while being the exact inversion
    of the shipped default. Where the direction matters it is a judgement, and
    TheFallbacksThatMustPointTheSafeWay below is where those get written down.
    """
    offenders = []
    for label, line, setting, value in found:
        if setting in RUNTIME_ASSIGNED or setting not in declared:
            continue
        if not value or value == "?":
            continue
        if value != declared[setting]:
            offenders.append(
                f"{label}:{line}: {setting} falls back to {value!r} but "
                f"config.py declares {declared[setting]!r}")
    return offenders


class NoFallbackContradictsConfig(unittest.TestCase):

    def test_every_fallback_matches_what_config_declares(self):
        offenders = contradictions(declared_defaults(), fallbacks())

        self.assertEqual(offenders, [],
                         "a fallback disagrees with config.py, so which one is "
                         "right depends on how the value happened to be read: "
                         + "; ".join(offenders))

    def test_the_rule_actually_detects_a_contradiction(self):
        """Control. Every other test here reads the real tree, where the answer
        is now "none" - which is also exactly what a rule that had stopped
        comparing anything at all would say."""
        declared = {"DEBUG_CHANNEL": "#example-serv", "MAX_DCC_SLOTS": 3}

        caught = contradictions(declared, [
            ("fake.py", 1, "DEBUG_CHANNEL", "#example-debug"),
            ("fake.py", 2, "MAX_DCC_SLOTS", 9),
        ])

        self.assertEqual(len(caught), 2, caught)
        self.assertIn("#example-debug", caught[0])

    def test_the_rule_passes_what_it_should(self):
        """The other direction. A rule that called everything a contradiction
        would be no more use than one that called nothing one."""
        declared = {"DEBUG_CHANNEL": "#example-serv", "MAX_DCC_SLOTS": 3}

        self.assertEqual(contradictions(declared, [
            ("fake.py", 1, "DEBUG_CHANNEL", "#example-serv"),   # agrees
            ("fake.py", 2, "DEBUG_CHANNEL", ""),             # declines
            ("fake.py", 3, "MAX_DCC_SLOTS", 0),              # declines
            ("fake.py", 4, "ORIGINAL_NICK", "DCCore"),       # runtime-assigned
            ("fake.py", 5, "NOT_A_SETTING", "x"),            # not declared
        ]), [])

    def test_the_scan_finds_the_fallbacks_it_is_meant_to_check(self):
        """Fixture invariant. A scan matching nothing would pass the test above
        on a file full of contradictions."""
        found = fallbacks()

        self.assertGreater(len(found), 40, "the getattr scan stopped matching")
        self.assertIn("DEBUG_CHANNEL", {setting for _l, _n, setting, _v in found})

    def test_config_declares_the_settings_being_compared_against(self):
        """The other half of the same invariant: an empty declaration table
        would make every comparison vacuous."""
        declared = declared_defaults()

        self.assertGreater(len(declared), 40)
        self.assertEqual(declared["DEBUG_CHANNEL"], config.DEBUG_CHANNEL)


class TheFallbacksThatMustPointTheSafeWay(unittest.TestCase):
    """Falsy fallbacks pass the rule above, because declining to answer is
    usually right. Where it is not, the direction is a judgement that has to be
    written down as one - the scan cannot make it."""

    def test_pause_on_update_defaults_to_pausing(self):
        """It was False at all three call sites: the exact inversion of the
        shipped default, and the unsafe direction. "Should I keep serving while
        the index is being rebuilt?" answers itself."""
        offenders = [f"{label}:{line}" for label, line, setting, value in fallbacks()
                     if setting == "PAUSE_ON_UPDATE" and value is not True]

        self.assertEqual(offenders, [],
                         "PAUSE_ON_UPDATE falls back to not pausing at: "
                         + ", ".join(offenders))

    def test_all_four_call_sites_are_still_there(self):
        """Fixture invariant: the test above passes trivially if the scan stops
        finding them. webserver.py's own PAUSE_ON_UPDATE check (added
        alongside the dashboard's "Update list" tool, in start_list_update())
        is the fourth - it decides whether an already-running system scan
        should block a dashboard-triggered rebuild the same way
        commands.handle_list_update_request() itself would."""
        sites = [s for _l, _n, s, _v in fallbacks() if s == "PAUSE_ON_UPDATE"]

        self.assertEqual(len(sites), 4,
                         "expected commands.py, dcc.py, list.py and webserver.py")


class AnAuthorisationCheckRefusesWhenItDoesNotKnow(DCCoreTestCase):
    """is_admin() with no ADMIN_NICK at all. The setting is always declared, so
    this cannot happen today - which is precisely why the old default sat there
    unexercised and unnoticed for as long as it did."""

    def without_admin_nick(self):
        previous = config.ADMIN_NICK
        del config.ADMIN_NICK
        self.addCleanup(setattr, config, "ADMIN_NICK", previous)

    def test_nobody_is_an_admin(self):
        self.without_admin_nick()

        for nick in ("sysop", "SysOp", "Op2", "mallory", ""):
            with self.subTest(nick=nick):
                self.assertFalse(commands.is_admin(nick))

    def test_the_nick_the_old_default_named_is_not_special(self):
        """The whole point. 'SysOp' was the default, so with the setting absent
        it was the one nick that still passed - and an Undernet nick is not
        owned without services auth, so anyone can simply take it."""
        self.without_admin_nick()

        self.assertFalse(commands.is_admin("sysop"))

    def test_an_empty_setting_grants_nothing_either(self):
        self.set_config(ADMIN_NICK="")

        self.assertFalse(commands.is_admin("sysop"))
        self.assertFalse(commands.is_admin("Op2"))

    def test_a_configured_admin_still_passes(self):
        """The control. Failing closed must not mean failing always."""
        self.set_config(ADMIN_NICK="SysOp,Op2")

        self.assertTrue(commands.is_admin("sysop"))
        self.assertTrue(commands.is_admin("OP2"))
        self.assertFalse(commands.is_admin("mallory"))


class NoDebugChannelMeansNoneIsJoined(unittest.TestCase):
    """irc.py sends JOIN with whatever this resolves to. An empty fallback on
    its own is not enough there - a bare JOIN with no channel is a malformed
    line - so the site has to check before sending rather than lean on the
    default."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_join_is_guarded_by_the_value_being_present(self):
        body = self.source()
        start = body.index("debug_chan = str(getattr(config, 'DEBUG_CHANNEL'")
        window = body[start:start + 400]

        self.assertIn("if debug_chan:", window,
                      "the JOIN is sent whatever the value is")
        self.assertLess(window.index("if debug_chan:"), window.index("JOIN {debug_chan}"),
                        "the check comes after the send")

    def test_the_operator_is_told_when_none_was_joined(self):
        """Silently not joining looks identical to joining and being refused."""
        self.assertIn("No DEBUG_CHANNEL is set", self.source())


if __name__ == "__main__":
    unittest.main()
