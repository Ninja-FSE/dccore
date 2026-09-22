"""SERVER accepted "host:port" and URL forms, and the bot then looped on a
bare getaddrinfo error every ten seconds (audit L23, #687).

The setup form refused only a space in SERVER, and PORT is not on the form,
so the natural first-timer spelling "irc.undernet.org:6667" - or a pasted
irc:// URL - was accepted, written, and connect() failed on name resolution
for ever: "[ERROR] Connection failed: [Errno 11001] getaddrinfo failed.
Reconnecting in 10 seconds..." with nothing saying the colon or the scheme
was the problem. The terminal setup (configure.py) and a hand-edited
settings.conf took the same values.

settings_file.server_problem() names what is wrong and what to write
instead; the form, configure.py's prompt and the settings.conf reader (so
the dashboard's Settings page too) all refuse through it.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests import test_set_it_up_in_the_browser as setup  # noqa: E402

GOOD = setup.GOOD


class TheChecker(unittest.TestCase):

    def test_the_audits_three_spellings_are_refused_and_told_why(self):
        self.assertIn("put 'irc.undernet.org' in SERVER and 6667 in PORT",
                      settings_file.server_problem("irc.undernet.org:6667"))
        self.assertIn("is a URL; SERVER is the host name alone, e.g. irc.undernet.org",
                      settings_file.server_problem("irc://irc.undernet.org"))
        self.assertIn("is a URL", settings_file.server_problem("http://irc.undernet.org"))

    def test_a_slash_a_colon_without_a_port_a_space_and_a_blank(self):
        self.assertIn("has a / in it", settings_file.server_problem("irc.undernet.org/6667"))
        self.assertIn("has a : in it", settings_file.server_problem("irc.undernet.org:irc"))
        self.assertIn("has a space in it", settings_file.server_problem("irc undernet"))
        self.assertIn("cannot be empty", settings_file.server_problem("  "))

    def test_a_host_name_passes(self):
        for host in ("irc.undernet.org", "localhost", "127.0.0.1", "eu.undernet.org", "my-irc.example.net"):
            self.assertIsNone(settings_file.server_problem(host), host)


class TheSetupForm(unittest.TestCase):

    def submit(self, server):
        return webserver.validate_setup_form(dict(GOOD, SERVER=server))

    def test_host_port_and_a_url_are_refused_with_one_message(self):
        for server in ("irc.undernet.org:6667", "irc://irc.undernet.org", "http://irc.undernet.org"):
            changes, _hash, errors = self.submit(server)

            self.assertNotIn("SERVER", changes, server)
            self.assertEqual([field for field, _m in errors], ["SERVER"], server)
            self.assertIn("no spaces, no port and no irc://", errors[0][1])
            self.assertIn("the port is a separate setting", errors[0][1])

    def test_in_spanish_and_french(self):
        for lang, must in (("es", "ni puerto, ni irc://"), ("fr", "ni port, ni irc://")):
            _c, _h, errors = webserver.validate_setup_form(dict(GOOD, SERVER="irc.undernet.org:6667"), lang)

            self.assertIn(must, errors[0][1], lang)

    def test_a_host_name_is_still_taken(self):
        changes, _hash, errors = self.submit("irc.undernet.org")

        self.assertEqual(errors, [])
        self.assertEqual(changes["SERVER"], "irc.undernet.org")


class TheSettingsFileReader(unittest.TestCase):

    def test_host_port_in_settings_conf_is_a_fault_that_names_the_fix(self):
        with self.assertRaises(settings_file.SettingsWriteError) as caught:
            settings_file._check_writable("SERVER", "irc.undernet.org:6667", {"SERVER": "x"}, {"SERVER": str})

        self.assertIn("not a server name", str(caught.exception))
        self.assertIn("6667 in PORT", str(caught.exception))

    def test_a_host_name_is_written(self):
        written = settings_file._check_writable("SERVER", "irc.undernet.org", {"SERVER": "x"}, {"SERVER": str})

        self.assertEqual(written, ("irc.undernet.org", "irc.undernet.org"))


class TheTerminalSetup(unittest.TestCase):

    def test_the_prompt_checks_through_the_same_function(self):
        with io.open(os.path.join(REPO_ROOT, "configure.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn('server = _ask("IRC server", default=_current("SERVER", "irc.undernet.org"),\n'
                      '                  check=settings_file.server_problem)', source)


if __name__ == "__main__":
    unittest.main()
