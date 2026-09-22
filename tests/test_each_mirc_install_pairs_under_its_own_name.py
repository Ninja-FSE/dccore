"""Every copy of the script paired as the literal "dccore.mrc" (audit M47,
#649).

The bot keeps one token per name, and pairing a name again replaces its
token - that is how a lost one is rotated. So with every copy of the script
called the same, pairing a laptop silently revoked the desktop: the
desktop's stored token was refused on its next reconnect, and until #601
made the script stop redialling on a refusal, three of those blocked the
shared home address for fifteen minutes with nothing but a stdout line on
the bot to say so.

The script now pairs as `dccore.mrc-<8 hex>`, the tail derived from the
mIRC folder it is loaded from, so two installs are two names. The bot side
needs no change and already does the right thing with two names; this
proves that, and reads the script for the name it sends.
"""

import io
import os
import re
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import db  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

DESKTOP = "dccore.mrc-3f9a12c0"
LAPTOP = "dccore.mrc-b7e04d55"


class TwoNamesAreTwoTokens(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        tmp = tempfile.mkdtemp(prefix="dccore-two-installs-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        real = db.ADMIN_TOKENS_FILE
        db.ADMIN_TOKENS_FILE = os.path.join(tmp, "tokens.json")
        self.addCleanup(setattr, db, "ADMIN_TOKENS_FILE", real)

    def pair(self, name):
        status, result = webserver.build_console_command_result("pair %s 1.1" % name, "127.0.0.1")
        self.assertEqual(status, 200)
        return [line.strip() for line in result["lines"] if line.startswith("  ")][0]

    def test_the_laptop_pairing_leaves_the_desktops_token_valid(self):
        desktop = self.pair(DESKTOP)
        laptop = self.pair(LAPTOP)

        self.assertEqual(adminchat._token_matches(desktop), DESKTOP)
        self.assertEqual(adminchat._token_matches(laptop), LAPTOP)

    def test_the_same_install_pairing_again_still_rotates_its_own(self):
        """The property the shared name was (accidentally) giving: a lost
        token is replaced by pairing again from the same place."""
        first = self.pair(DESKTOP)
        second = self.pair(DESKTOP)

        self.assertIsNone(adminchat._token_matches(first))
        self.assertEqual(adminchat._token_matches(second), DESKTOP)

    def test_and_the_bot_lists_both(self):
        self.pair(DESKTOP)
        self.pair(LAPTOP)
        status, result = webserver.build_console_command_result("unpair", "127.0.0.1")

        listed = " ".join(result["lines"])
        self.assertIn(DESKTOP, listed)
        self.assertIn(LAPTOP, listed)


class TheScriptSendsItsOwnName(unittest.TestCase):

    def script(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"),
                     encoding="ascii", newline="") as handle:
            return handle.read()

    def test_the_name_is_derived_from_the_install_folder(self):
        alias = re.search(r"^alias dccore\.client \{ return (.+) \}\r?$", self.script(), re.M)

        self.assertIsNotNone(alias)
        self.assertIn("$mircdir", alias.group(1), "per installation means the folder it is loaded from")
        self.assertTrue(alias.group(1).startswith("dccore.mrc- $+ "), alias.group(1))

    def test_pair_and_unpair_use_it_and_never_the_literal(self):
        text = self.script()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(";"))

        self.assertEqual(len(re.findall(r"dccore\.send pair \$dccore\.client", code)), 2,
                         "the two pair sends (on /dccore pair, and at the password prompt)")
        self.assertIn("dccore.send unpair $dccore.client", code)
        self.assertNotRegex(code, r"dccore\.send (un)?pair dccore\.mrc\b",
                            "a copy still pairing under the shared literal name")

    def test_hello_still_names_the_client_type(self):
        """The feed's `hello <client> <version>` is what kind of client this
        is, not which copy - the bot's log and the console page show it."""
        self.assertIn("dccore.send hello dccore.mrc $dccore.ver", self.script())

    def test_everything_it_uses_is_mirc_6(self):
        """$md5 and $mircdir are both mIRC 6.0-era identifiers; the header
        promises 6.10."""
        alias = re.search(r"^alias dccore\.client \{ return (.+) \}", self.script(), re.M).group(1)
        for ident in re.findall(r"\$[a-z0-9]+", alias):
            self.assertIn(ident, ("$left", "$md5", "$mircdir"), ident)


if __name__ == "__main__":
    unittest.main()
