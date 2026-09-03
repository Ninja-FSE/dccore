"""A hard-ban pattern that matches everyone must be refused, in every spelling.

The guard was originally "is anything left after removing the stars", on the
reasoning that adminchat.is_admin_host() matches a HOST pattern, and a host
contains no "!" or "@", so "*" was believed to be the only way to spell
"everything" there. That reasoning missed the dot: a host is dot-separated
labels, so "*.*" reduces to a lone "." under a stars-only strip - truthy, so
it was accepted, and compiled to a pattern matching essentially every real
host (#218). Both guards now strip the same four characters, "*!@.": the two
adminchat's own host string can never contain are harmless no-ops there, and
security.py needs all four for a full <nick>!<ident>@<host> hostmask.

security.py inherited the stars-only line for a pattern that can be a full
hostmask, and the same gap existed there first. Before #168, hostmask patterns
matched nothing at all, so it did not matter - "*!*@*" was inert for the same
reason "*!*@spammer.net" was inert, which was the bug #168 fixed. Making
hostmask bans work made "ban everyone" work too:

    *!*@*      residue "!@"   truthy, so accepted
    *!*        residue "!"    truthy, so accepted
    *@*        residue "@"    truthy, so accepted
    *!*@*.*    residue "!@."  truthy, so accepted

hard_bans.txt is persistent, so the recovery is to find and hand-edit the file.
_cmd_ban's own usage line is "e.g. ban *!*@spammer.net" - one slip away.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import security  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

# Every spelling of "match everything". A hostmask is <nick>!<ident>@<host>,
# so any pattern built only from wildcards and those separators (plus the dots
# that split a hostname) matches every user on the network.
MATCHES_EVERYONE = ("*", "**", "*!*", "*@*", "*!*@*", "*!*@*.*", "*.*")

# Patterns that are broad but legitimate, and must keep working.
LEGITIMATE = (
    "*!*@spammer.net",        # the form _cmd_ban's usage line documents
    "*!*@*.spammer.net",      # a whole domain
    "*!*@1.2.3.4",            # one address
    "*!~mallory@*",           # one ident everywhere
    "spammer",                # a plain nick
    "bad*",                   # a nick prefix
)


class BanCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(HARD_BANS_FILE=os.path.join(self.tree.root, "hard_bans.txt"))
        security._ban_notified.clear()
        self.addCleanup(security._ban_notified.clear)

    def ban(self, *patterns):
        with io.open(config.HARD_BANS_FILE, "w", encoding="utf-8") as handle:
            handle.write("\n".join(patterns) + "\n")

    def blocked(self, nick="innocent", host="~i@good.isp.net"):
        security._ban_notified.clear()
        return not security.check_user_status(nick, host)


class APatternThatMatchesEveryoneIsRefused(BanCase):

    def test_no_spelling_of_ban_everyone_gets_through(self):
        for pattern in MATCHES_EVERYONE:
            with self.subTest(pattern=pattern):
                self.ban(pattern)

                self.assertFalse(self.blocked(),
                                 f"{pattern!r} banned an innocent user")

    def test_it_says_so_rather_than_ignoring_it_quietly(self):
        """An operator who typed one and saw nothing would reasonably think
        the ban took."""
        import contextlib
        self.ban("*!*@*")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            security.check_user_status("innocent", "~i@good.isp.net")

        printed = buffer.getvalue()
        self.assertIn("over-broad", printed)
        self.assertIn("*!*@*", printed)

    def test_a_broad_pattern_does_not_disable_the_rest_of_the_file(self):
        """Skipping the bad line must not skip the good ones after it - the
        same shape as #12, where one malformed line dropped every ban below."""
        self.ban("*!*@*", "spammer")

        self.assertFalse(self.blocked("innocent", "~i@good.isp.net"))
        self.assertTrue(self.blocked("spammer", "~s@anywhere.net"))


class TheLegitimateOnesStillWork(BanCase):
    """A guard that refused every broad pattern would be no more use than one
    that refused none - #168 exists precisely to make these fire."""

    def test_every_documented_form_still_bans(self):
        cases = {
            "*!*@spammer.net": ("mallory", "~m@spammer.net"),
            "*!*@*.spammer.net": ("mallory", "~m@mail.spammer.net"),
            "*!*@1.2.3.4": ("mallory", "~m@1.2.3.4"),
            "*!~mallory@*": ("mallory", "~mallory@anywhere.net"),
            "spammer": ("spammer", "~s@good.isp.net"),
            "bad*": ("badguy", "~b@good.isp.net"),
        }
        for pattern, (nick, host) in cases.items():
            with self.subTest(pattern=pattern):
                self.ban(pattern)

                self.assertTrue(self.blocked(nick, host),
                                f"{pattern!r} stopped working")

    def test_and_they_still_only_hit_who_they_name(self):
        for pattern in LEGITIMATE:
            with self.subTest(pattern=pattern):
                self.ban(pattern)

                self.assertFalse(self.blocked("dave", "~d@good.isp.net"))


class TheAdminHostGuardCatchesDotsToo(unittest.TestCase):
    """adminchat.is_admin_host() carries the same shape of guard as the
    hard-ban one above, for the same reason - see this file's own docstring
    for why a stars-only strip missed "*.*" here specifically (#218)."""

    def _check(self, pattern, line):
        import adminchat
        previous = getattr(config, "ADMIN_HOSTMASKS", [])
        config.ADMIN_HOSTMASKS = [pattern]
        try:
            return adminchat.is_admin_host(line)
        finally:
            config.ADMIN_HOSTMASKS = previous

    def test_a_host_pattern_of_stars_is_refused(self):
        self.assertFalse(self._check("*", ":d!~d@any.host PRIVMSG x :y"))

    def test_no_spelling_of_match_everyone_gets_through(self):
        for pattern in ("*", "**", "*.*", "*.*.*"):
            with self.subTest(pattern=pattern):
                self.assertFalse(
                    self._check(pattern, ":d!~d@any.host PRIVMSG x :y"),
                    f"{pattern!r} admitted a host it should have refused")

    def test_and_a_real_host_pattern_still_admits(self):
        self.assertTrue(self._check(
            "operator2.users.undernet.org",
            ":c!~c@operator2.users.undernet.org PRIVMSG x :y"))
        self.assertFalse(self._check(
            "operator2.users.undernet.org",
            ":m!~m@evil.example PRIVMSG x :y"))

    def test_a_legitimate_wildcard_domain_still_admits(self):
        """The control for the fix itself: *.example.org names a real,
        legitimate set of hosts and must keep working."""
        self.assertTrue(self._check(
            "*.example.org", ":c!~c@shell.example.org PRIVMSG x :y"))
        self.assertFalse(self._check(
            "*.example.org", ":m!~m@evil.example PRIVMSG x :y"))


class TheSampleDoesNotPutTheDashboardOnTheLan(unittest.TestCase):
    """admin_config.py.sample is the file an operator copies. Its own comment
    says "127.0.0.1 is the tracked default: safe out of the box... Set this to
    0.0.0.0 here if you want it reachable from your LAN" - and the line below
    it set 0.0.0.0, doing the thing it had just described as opt-in."""

    def sample(self):
        with io.open(os.path.join(REPO_ROOT, "admin_config.py.sample"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_the_sample_binds_loopback(self):
        import ast
        for line in self.sample().splitlines():
            stripped = line.strip()
            if stripped.startswith("WEBUI_HOST"):
                value = ast.literal_eval(stripped.split("=", 1)[1].strip())

                self.assertEqual(value, "127.0.0.1")
                return
        self.fail("the sample no longer sets WEBUI_HOST at all")

    def test_it_still_explains_how_to_reach_it_from_the_lan(self):
        """Loopback by default is not the same as hiding the option."""
        self.assertIn("0.0.0.0", self.sample())

    def test_no_uncommented_sample_value_is_less_safe_than_config(self):
        """The general rule. The sample may differ from config.py - it is a
        template, and enabling the dashboard is a reasonable thing for it to
        demonstrate - but not on a setting where its value is the riskier one.
        """
        import ast
        declared = {}
        with io.open(os.path.join(REPO_ROOT, "defaults.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target, value = node.target.id, node.value
            elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                target, value = node.targets[0].id, node.value
            else:
                continue
            try:
                declared[target] = ast.literal_eval(value)
            except Exception:
                continue

        # Settings where one value is defensibly safer than the other.
        SAFER = {"WEBUI_HOST": "127.0.0.1"}
        offenders = []
        for line in self.sample().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name = stripped.split("=")[0].strip()
            if name not in SAFER:
                continue
            try:
                value = ast.literal_eval(stripped.split("=", 1)[1].strip())
            except Exception:
                continue
            if value != SAFER[name]:
                offenders.append(f"{name}={value!r} (config.py ships "
                                 f"{declared.get(name)!r})")

        self.assertEqual(offenders, [], "the sample ships a riskier value than "
                                        "config.py for: " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
