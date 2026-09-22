"""The token lived in clear text in dccore.ini and the docs said a stolen
.mrc was the exposure (audit L19, #683).

hsave writes mIRC's hash table as plain item/value text, so the console
token sits readable in dccore.ini beside the script. The script header and
the guide said where it was kept but never that it was clear text, and the
guide's pairing section named "a stolen .mrc" as the cost when the .mrc
carries nothing - the file that matters is dccore.ini. An operator who
zipped their mIRC folder to share the script shipped the token with it,
pointed at the wrong file by the threat model. A wording fix, no code
change; .gitignore already keeps dccore.ini out of the tree (#575).
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def read(relative):
    with io.open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


class TheScriptSaysSo(unittest.TestCase):

    def test_the_header_names_the_file_and_that_it_is_clear_text(self):
        header = read("scripts/mirc/dccore.mrc").split("alias dccore.ini", 1)[0]

        self.assertIn("dccore.ini is CLEAR TEXT", header)
        self.assertIn("Treat that file as you would a", header)
        self.assertIn("password file", header)
        self.assertIn("/dccore unpair the moment you think", header)

    def test_the_pairing_message_says_it_at_the_moment_the_token_is_stored(self):
        script = read("scripts/mirc/dccore.mrc")

        self.assertIn("The token is kept in dccore.ini beside the script, in clear text - keep that file as you would a password", script)

    def test_the_store_is_still_a_plain_hsave(self):
        """The wording describes what the code does; if the store ever
        changes (hsave -b, or an obfuscated value), this and the wording
        must change together."""
        script = read("scripts/mirc/dccore.mrc")

        self.assertIn("alias dccore.save { hsave -o dccore $dccore.ini }", script)


class TheGuideSaysSo(unittest.TestCase):

    def test_the_cost_is_named_for_the_right_file(self):
        guide = read("docs/ADMIN-CONSOLE.md")

        self.assertNotIn("a stolen `.mrc`", guide)
        self.assertIn("so a stolen token costs you a console session and nothing more", guide)
        self.assertIn("`dccore.ini` beside the script, and it is clear text", guide)
        self.assertIn("The `.mrc` itself carries nothing.", guide)
        self.assertIn("Keep `dccore.ini`\nas you would a password file", guide)

    def test_the_pairing_walkthrough_points_at_it(self):
        guide = read("docs/ADMIN-CONSOLE.md")

        self.assertIn("`dccore.ini` beside the script (in clear\ntext - see \"What a token does not do\" above)", guide)


class TheFileStaysOutOfTheTree(unittest.TestCase):

    def test_gitignore_keeps_dccore_ini(self):
        self.assertIn("scripts/mirc/dccore.ini", read(".gitignore"))


if __name__ == "__main__":
    unittest.main()
