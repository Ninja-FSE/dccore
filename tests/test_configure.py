"""configure.py - guided first-run configuration. #170's RFC.

The interactive orchestration (collect_answers()) is exercised end-to-end by
monkeypatching input()/adminchat._read_password() with canned answers, the
same idiom used throughout this suite for anything that would otherwise need
a real terminal. Everything else - the text edit, the write paths - is
tested as the pure functions configure.py already pulls out for exactly this
reason.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import configure  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class BuildAdminConfigTextTests(unittest.TestCase):
    """The pure text edit, no file I/O."""

    def test_a_fresh_file_gets_the_line_appended(self):
        text = configure.build_admin_config_text("# admin_config.py\n", "HASH123")
        self.assertIn('ADMIN_PASSWORD_HASH = "HASH123"', text)
        self.assertTrue(text.startswith("# admin_config.py\n"))

    def test_an_existing_assignment_is_replaced_in_place_not_duplicated(self):
        existing = (
            "ADMIN_HOSTMASKS = [\"\"]\n"
            "ADMIN_PASSWORD_HASH = \"OLDHASH\"\n"
            "ADMIN_CHAT_MODE = \"auto\"\n"
        )
        text = configure.build_admin_config_text(existing, "NEWHASH")

        self.assertEqual(text.count("ADMIN_PASSWORD_HASH ="), 1)
        self.assertIn('ADMIN_PASSWORD_HASH = "NEWHASH"', text)
        self.assertNotIn("OLDHASH", text)
        # Everything else survives untouched.
        self.assertIn('ADMIN_HOSTMASKS = [""]', text)
        self.assertIn('ADMIN_CHAT_MODE = "auto"', text)

    def test_a_missing_trailing_newline_does_not_glue_lines_together(self):
        text = configure.build_admin_config_text("ADMIN_HOSTMASKS = []", "HASH1")
        self.assertIn("ADMIN_HOSTMASKS = []\nADMIN_PASSWORD_HASH", text)

    def test_an_empty_starting_file_still_produces_a_valid_assignment(self):
        text = configure.build_admin_config_text("", "HASH1")
        self.assertEqual(text, 'ADMIN_PASSWORD_HASH = "HASH1"\n')


class WriteAdminConfigPasswordTests(unittest.TestCase):
    """The file I/O wrapper, against real temp files - never the repo's own
    admin_config.py/admin_config.py.sample, via the path/sample_path
    overrides configure.py's own functions accept for exactly this reason."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dccore-setup-test-")
        self.path = os.path.join(self.tmp, "admin_config.py")
        self.sample_path = os.path.join(self.tmp, "admin_config.py.sample")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_from_the_sample_when_neither_real_file_exists(self):
        with open(self.sample_path, "w", encoding="utf-8") as handle:
            handle.write("ADMIN_HOSTMASKS = [\"\"]\n# Generate with: python adminchat.py\nADMIN_PASSWORD_HASH = \"\"\n")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            configure.write_admin_config_password("REALHASH", path=self.path,
                                              sample_path=self.sample_path)

        with open(self.path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn('ADMIN_PASSWORD_HASH = "REALHASH"', written)
        self.assertIn('ADMIN_HOSTMASKS = [""]', written)

    def test_creates_a_bare_file_when_neither_real_file_nor_sample_exists(self):
        configure.write_admin_config_password("REALHASH", path=self.path,
                                          sample_path=self.sample_path)

        with open(self.path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn('ADMIN_PASSWORD_HASH = "REALHASH"', written)

    def test_a_real_existing_file_is_edited_not_replaced_from_the_sample(self):
        with open(self.sample_path, "w", encoding="utf-8") as handle:
            handle.write("ADMIN_HOSTMASKS = [\"\"]\nADMIN_PASSWORD_HASH = \"\"\n")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("ADMIN_HOSTMASKS = [\"real.host.example\"]\n"
                         "ADMIN_PASSWORD_HASH = \"OLDHASH\"\n")

        configure.write_admin_config_password("NEWHASH", path=self.path,
                                          sample_path=self.sample_path)

        with open(self.path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn('ADMIN_PASSWORD_HASH = "NEWHASH"', written)
        self.assertIn("real.host.example", written,
                      "an existing real admin_config.py must never be "
                      "replaced by the sample's own placeholder content")


class WriteSettingsConfTests(DCCoreTestCase):
    """write_settings_conf() - the thin wrapper over settings_file.save()."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-setup-settings-test-")
        self.path = os.path.join(self.tmp, "settings.conf")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_writes_every_answer(self):
        answers = {
            "NICKNAME": "MyBot",
            "SERVER": "irc.undernet.org",
            "CHANNEL": "#my-channel",
            "ADMIN_NICK": "MyAdmin",
            "FILE_DIRECTORY": "/tmp/some-music-folder",
        }
        configure.write_settings_conf(answers, path=self.path)

        with open(self.path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn("NICKNAME = MyBot", written)
        self.assertIn("CHANNEL = #my-channel", written)
        self.assertIn("ADMIN_NICK = MyAdmin", written)

    def test_writes_exactly_the_dict_it_was_given(self):
        """No filtering happens at this layer any more - collect_answers()
        already decided what belongs in `changes` (a blank SERVER, or the
        dashboard left off, are both simply absent from the dict, never
        written as an explicit "no"). A key genuinely absent from `changes`
        must not appear in the file at all."""
        configure.write_settings_conf({"NICKNAME": "MyBot"}, path=self.path)

        with open(self.path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn("NICKNAME = MyBot", written)
        self.assertNotIn("SERVER", written)
        self.assertNotIn("WEBUI", written)


class CurrentValueTests(DCCoreTestCase):
    """_current() - what a re-run's prompts default to."""

    def test_returns_the_resolved_value_when_set(self):
        self.set_config(NICKNAME="AlreadyConfigured")
        self.assertEqual(configure._current("NICKNAME"), "AlreadyConfigured")

    def test_falls_back_when_the_shipped_value_is_blank(self):
        """NICKNAME/CHANNEL/ADMIN_NICK/FILE_DIRECTORY ship None - #170's RFC.
        A blank value must fall back to the caller's default, not surface
        "None" as a literal prompt default."""
        self.set_config(CHANNEL=None)
        self.assertEqual(configure._current("CHANNEL", "fallback"), "fallback")

    def test_falls_back_to_empty_string_when_no_fallback_given(self):
        self.set_config(CHANNEL=None)
        self.assertEqual(configure._current("CHANNEL"), "")


class CollectAnswersEndToEndTests(DCCoreTestCase):
    """The full interactive flow, with input()/adminchat._read_password()
    replaced by canned answers - no real terminal needed, matching every
    other interactive-console test in this suite."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self._real_read_password = adminchat._read_password
        self.addCleanup(setattr, adminchat, "_read_password", self._real_read_password)

    def _run_with_answers(self, answers, passwords=("secret123", "secret123")):
        answer_iter = iter(answers)
        password_iter = iter(passwords)

        def fake_input(prompt=""):
            return next(answer_iter)

        def fake_read_password(prompt):
            return next(password_iter)

        adminchat._read_password = fake_read_password
        import builtins
        real_input = builtins.input
        builtins.input = fake_input
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                return configure.collect_answers()
        finally:
            builtins.input = real_input

    def test_a_clean_run_produces_every_required_field(self):
        answers, password_hash = self._run_with_answers([
            "MyBot",           # nickname
            "",                # server (accept default)
            "#my-channel",     # channel
            "MyAdmin",         # admin nick
            self.tree.music,   # file directory (exists already via make_tree)
            "n",               # web dashboard: skip
        ])

        self.assertEqual(answers["NICKNAME"], "MyBot")
        self.assertEqual(answers["SERVER"], "irc.undernet.org")
        self.assertEqual(answers["CHANNEL"], "#my-channel")
        self.assertEqual(answers["ADMIN_NICK"], "MyAdmin")
        self.assertEqual(answers["FILE_DIRECTORY"], self.tree.music)
        self.assertEqual(answers["WEBUI_ENABLED"], False)
        self.assertNotIn("WEBUI_HOST", answers,
                         "WEBUI_HOST must not be written at all when the "
                         "dashboard was not enabled")
        self.assertTrue(password_hash)
        self.assertTrue(adminchat.verify_password(password_hash, "secret123"))

    def test_a_blank_required_field_is_reprompted_not_accepted(self):
        """A genuinely fresh install (NICKNAME still unset - DCCoreTestCase's
        own baseline sets it to "DCCore" for every OTHER test, so this one
        blanks it explicitly) must not accept a blank first answer - REQUIRED
        refuses to boot on blank either way, but failing loudly here is much
        earlier and much clearer than a bare startup refusal afterwards."""
        self.set_config(NICKNAME=None)
        answers, _hash = self._run_with_answers([
            "",                # blank - must be rejected and re-asked
            "SecondTry",
            "",
            "#chan",
            "Admin",
            self.tree.music,
            "n",
        ])
        self.assertEqual(answers["NICKNAME"], "SecondTry")

    def test_mismatched_passwords_are_reprompted(self):
        answers, password_hash = self._run_with_answers(
            ["MyBot", "", "#chan", "Admin", self.tree.music, "n"],
            passwords=("first-password", "different-password", "matched", "matched"))

        self.assertTrue(adminchat.verify_password(password_hash, "matched"))
        self.assertFalse(adminchat.verify_password(password_hash, "first-password"))

    def test_enabling_the_dashboard_asks_about_lan_access(self):
        answers, _hash = self._run_with_answers([
            "MyBot", "", "#chan", "Admin", self.tree.music,
            "y",   # enable the dashboard
            "n",   # localhost only
        ])
        self.assertEqual(answers["WEBUI_ENABLED"], True)
        self.assertEqual(answers["WEBUI_HOST"], "127.0.0.1")

    def test_enabling_lan_access_sets_the_lan_host(self):
        answers, _hash = self._run_with_answers([
            "MyBot", "", "#chan", "Admin", self.tree.music,
            "y",   # enable the dashboard
            "y",   # reachable from the LAN
        ])
        self.assertEqual(answers["WEBUI_ENABLED"], True)
        self.assertEqual(answers["WEBUI_HOST"], "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
