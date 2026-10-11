"""#1272: a settings.conf parse error named the wrong line, and a section
that is not in the file.

parse() puts a synthetic "[__dccore__]" header in front of the text before
configparser reads it, so every line number configparser reported was one
too high, and a duplicate key was said to be in section "__dccore__". The
operator was sent to the wrong line of the one file whose single error had
just reverted every setting to its default.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import settings_file  # noqa: E402


def error_of(text):
    try:
        settings_file.parse(text)
    except settings_file.SettingsError as err:
        return str(err)
    raise AssertionError(f"parse() accepted {text!r}")


class TheLineIsTheFilesOwn(unittest.TestCase):

    def test_a_line_without_an_equals_sign(self):
        message = error_of("# my settings\nNICKNAME = SampleBot\nCHANNEL #example-room\n")
        self.assertIn("line 3 ", message)
        self.assertNotIn("line 4", message)
        self.assertIn("CHANNEL #example-room", message)

    def test_on_the_first_line(self):
        message = error_of("MAX_DCC_SLOTS 5\n")
        self.assertTrue(message.startswith("line 1 "), message)

    def test_every_bad_line_is_counted(self):
        message = error_of("A 1\nNICKNAME = SampleBot\nB 2\nC 3\n")
        self.assertTrue(message.startswith("line 1 "), message)
        self.assertIn("Lines 3, 4 have the same problem", message)

    def test_a_key_set_twice_names_the_second_line(self):
        message = error_of("NICKNAME = SampleBot\nCHANNEL = #example-room\nNICKNAME = OtherBot\n")
        self.assertIn("line 3 ", message)
        self.assertIn("NICKNAME", message)

    def test_a_section_heading_repeated(self):
        message = error_of("[Web]\nWEBUI_PORT = 8420\n[Web]\nWEBUI_HOST = 127.0.0.1\n")
        self.assertIn("line 3 ", message)
        self.assertIn("[Web]", message)

    def test_the_lines_agree_with_the_indented_line_check(self):
        """That check counted correctly all along; now both say the same line."""
        indented = error_of("NICKNAME = SampleBot\n    stray\n")
        broken = error_of("NICKNAME = SampleBot\nstray\n")
        self.assertIn("line 2 ", indented)
        self.assertIn("line 2 ", broken)


class TheInternalSectionIsNeverNamed(unittest.TestCase):

    def test_none_of_the_messages_mention_it(self):
        for text in ("NICKNAME = SampleBot\nNICKNAME = OtherBot\n",
                     "CHANNEL #example-room\n",
                     "NICKNAME = SampleBot\n[IRC]\nNICKNAME = OtherBot\n"):
            with self.subTest(text=text):
                self.assertNotIn("__dccore__", error_of(text))

    def test_a_key_in_two_sections_says_the_top_of_the_file(self):
        message = error_of("NICKNAME = SampleBot\n[IRC]\nNICKNAME = OtherBot\n")
        self.assertIn("at the top of the file", message)
        self.assertIn("[IRC]", message)


class TheStartupLogSaysIt(unittest.TestCase):

    def test_apply_to_reports_the_real_line(self):
        import tempfile
        import shutil
        home = tempfile.mkdtemp(prefix="dccore-parse-line-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        path = os.path.join(home, "settings.conf")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("NICKNAME = SampleBot\nMAX_DCC_SLOTS 5\n")
        said = []
        report = settings_file.apply_to({"NICKNAME": "", "MAX_DCC_SLOTS": 3},
                                        path=path, log=said.append)
        self.assertIn("line 2 ", report["read_error"])
        self.assertTrue(any("line 2 " in line for line in said), said)


if __name__ == "__main__":
    unittest.main()
