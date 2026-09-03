"""Importing webserver.py must not drag in the daemon.

webserver.py imports `list` lazily, inside its handlers, and says why in a
comment: so that importing the module does not pull in oserve/dcc/announce,
which is what lets tests/test_webserver.py exercise the routes on their own
without a running bot.

That property was written down and enforced by nothing. It survived every
change to this file so far by luck and care rather than by check - PR #131's
`import adminchat` at module scope was fine, but only because adminchat
happens to import nothing heavier than config and platform_compat, and the
next one might not be. The failure would not look like this either: it would
look like an unrelated webserver test suddenly needing a daemon, or a CI job
starting a thread nobody asked for.

WHY A SUBPROCESS

Because this suite has already imported half the daemon by the time any test
runs. Checking sys.modules in-process would pass no matter what webserver.py
did, which is the same shape as a check that agrees with itself - the failure
mode this file exists to prevent, applied to its own method. A clean
interpreter is the only place the question means anything.
"""

import io
import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What webserver.py may pull in from this repository, and nothing else.
#
# This is an allow-list rather than a list of banned modules on purpose: a new
# import lands as a failure that has to be looked at, instead of slipping
# through because nobody thought to ban it in advance. Adding a name here is
# the deliberate decision, and the question to answer first is whether that
# module is inert at import time - no threads, no sockets, no work.
#
#   config, settings_file  - the settings themselves, and the file that
#                            applies settings.conf over them
#   runtime                - live containers, imported by config
#   adminchat              - verify_password() for the dashboard login (#131);
#                            stdlib plus config and platform_compat only
#   platform_compat        - stdlib wrappers, imported by adminchat
ALLOWED = {
    "webserver",
    "defaults",
    "runtime",
    "settings_file",
    "adminchat",
    "platform_compat",
}


def repo_modules():
    return {name[:-3] for name in os.listdir(REPO_ROOT)
            if name.endswith(".py") and not name.startswith("_")}


def modules_after_importing(target):
    """Every module of this repository present after `import <target>` in a
    fresh interpreter."""
    known = repo_modules()
    code = (
        "import sys, json\n"
        "import %s\n"
        "known = %r\n"
        "print(json.dumps(sorted(m for m in sys.modules if m in known)))\n"
        % (target, known)
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            "importing %s in a clean interpreter failed:\n%s"
            % (target, result.stderr.strip()[-2000:]))
    return set(json.loads(result.stdout))


class ImportingWebserverStaysLight(unittest.TestCase):

    def test_it_pulls_in_nothing_outside_the_allow_list(self):
        loaded = modules_after_importing("webserver")

        unexpected = sorted(loaded - ALLOWED)

        self.assertEqual(
            unexpected, [],
            "importing webserver.py now pulls in " + ", ".join(unexpected) +
            ". tests/test_webserver.py exercises the routes without a running "
            "daemon, and webserver.py imports `list` lazily to keep that true. "
            "If the new import is genuinely inert - no threads, no sockets, no "
            "work at import time - add it to ALLOWED with a note saying so. If "
            "it is not, import it inside the handler that needs it, the way "
            "`list` is imported.")

    def test_the_daemon_itself_stays_out(self):
        """Named explicitly, because these are the ones whose arrival would
        actually start something: threads, listeners, an IRC connection."""
        loaded = modules_after_importing("webserver")

        daemon = {"oserve", "irc", "dcc", "dcc_fetch", "announce", "commands",
                  "queue_mgr", "update_list", "security", "db", "list",
                  "list_fetch", "stats_mgr"}

        self.assertEqual(sorted(loaded & daemon), [],
                         "the daemon is now imported by webserver.py")

    def test_the_allow_list_has_not_gone_stale(self):
        """Control. A name left in ALLOWED after its import is gone is not a
        bug, but it makes the list read as permission for something nobody
        chose - and this list is only useful while it means something."""
        loaded = modules_after_importing("webserver")

        # adminchat arrives with #131's login gate; platform_compat only comes
        # with it. Neither is stale before that lands, so both are exempt from
        # this check rather than making it fail on either side of that merge.
        pending = {"adminchat", "platform_compat"}
        stale = sorted(ALLOWED - loaded - pending)

        self.assertEqual(
            stale, [],
            "ALLOWED lists module(s) webserver.py no longer imports: " +
            ", ".join(stale) + ". Remove them, so the list stays a statement "
            "about what is actually reachable.")

    def test_the_scan_can_see_the_repository(self):
        """Fixture invariant. If repo_modules() ever returned nothing, both
        tests above would pass while checking nothing at all."""
        names = repo_modules()

        self.assertIn("webserver", names)
        self.assertIn("oserve", names)
        self.assertGreater(len(names), 10, sorted(names))


if __name__ == "__main__":
    unittest.main()
