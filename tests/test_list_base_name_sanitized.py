"""#427: an IRC-legal nickname made LIST_BASE_NAME an illegal Windows path.

defaults.py derives LIST_BASE_NAME from NICKNAME whenever LIST_BASE_NAME is
still the shipped literal. "|" is an ordinary, common nick character
(RFC 2812's specials are []\\`_^{|}, and "Bot|Away" is one of the commonest
nick shapes on the network) and "\\" is a legal nick character too, but NTFS
treats both as illegal in a path. Derived with no sanitiser, either one
reached update_list.py's staging open() and made every scheduled rebuild
fail - forever, on a fresh install - on Windows only; the identical run
succeeds on Linux because both characters are legal POSIX filenames.

Reproduced here the same way the audit that found it did: write a real
settings.conf with the offending NICKNAME, reload defaults.py against it
exactly as startup would, and read back what LIST_BASE_NAME actually became.
"""

import contextlib
import importlib
import io
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402


class ItDerivesAWindowsSafeName(unittest.TestCase):

    def _reload_config_with(self, nickname):
        tmp = tempfile.mkdtemp(prefix="dccore-427-test-")
        settings_path = os.path.join(tmp, "settings.conf")
        with io.open(settings_path, "w", encoding="utf-8") as handle:
            handle.write(f"NICKNAME = {nickname}\n")

        self.addCleanup(self._quiet_reload)
        self.addCleanup(os.environ.pop, "DCCORE_SETTINGS_FILE", None)
        import shutil
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        os.environ["DCCORE_SETTINGS_FILE"] = settings_path
        self._quiet_reload()
        return config

    def _quiet_reload(self):
        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(config)

    def test_a_pipe_in_the_nick_does_not_survive_into_the_base_name(self):
        """The exact shape from the report: "Bot|Away"-style nicks are
        common, and NTFS refuses "|" in a filename."""
        cfg = self._reload_config_with("Serv|NL")

        self.assertNotIn("|", cfg.LIST_BASE_NAME)

    def test_a_backslash_in_the_nick_does_not_survive_either(self):
        """A literal backslash is IRC-legal but NTFS reads it as a
        directory separator - update_list.py's own repro used "Bot\\Home"
        and got ENOENT, not ENOTDIR, because the write landed in a path
        nobody created."""
        cfg = self._reload_config_with("Bot\\Home")

        self.assertNotIn("\\", cfg.LIST_BASE_NAME)

    def test_an_ordinary_nick_is_left_exactly_as_it_is(self):
        """The fix must not rename every operator's list out from under
        them - only the two characters that are actually unsafe are
        anyone's business to change."""
        cfg = self._reload_config_with("DCCoreTest")

        self.assertEqual(cfg.LIST_BASE_NAME, "DCCoreTest")

    def test_other_legal_nick_specials_still_pass_through(self):
        """RFC 2812's specials are []\\`_^{|} - only "|" and "\\" are
        unsafe on NTFS. A blanket sanitiser that also mangled "[", "]",
        "{", "}", "^" or "`" would rename lists that were never broken."""
        cfg = self._reload_config_with("Nick[Away]^`{x}")

        self.assertEqual(cfg.LIST_BASE_NAME, "Nick[Away]^`{x}")


if __name__ == "__main__":
    unittest.main()
