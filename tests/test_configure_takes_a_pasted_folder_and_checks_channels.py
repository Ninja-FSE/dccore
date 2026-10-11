"""#1272: two answers configure.py took as typed.

The music folder. Explorer's "Copy as path" always adds double quotes, and
dragging a folder onto a terminal quotes it too, so an existing folder was
reported as not existing, the offer to create it failed with WinError 123,
and the question looped. On Linux and macOS answering y created a folder
literally named with the quotes, relative to wherever configure.py ran,
and wrote that as FILE_DIRECTORY.

The channels. "music" and "#music #rock" were written to settings.conf; the
bot then sent "JOIN music" or joined #music with "#rock" as its key. The
browser setup refused both - now all three use settings_file's one rule.
"""

import builtins
import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import configure  # noqa: E402
import settings_file  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class Prompted(DCCoreTestCase):

    def answer(self, answers, call):
        feed = iter(answers)
        real_input = builtins.input
        builtins.input = lambda prompt="": next(feed)
        self.addCleanup(setattr, builtins, "input", real_input)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = call()
        self.said = buffer.getvalue()
        self.assertEqual(list(feed), [], "not every answer was asked for")
        return result


class TheQuotesComeOff(unittest.TestCase):

    def test_double_quotes_from_copy_as_path(self):
        self.assertEqual(settings_file.unquote_path('"C:\\Users\\sample\\My Music"', posix=False),
                         "C:\\Users\\sample\\My Music")

    def test_single_quotes_from_a_linux_drag(self):
        self.assertEqual(settings_file.unquote_path("'/home/sample/My Music' ", posix=True),
                         "/home/sample/My Music")

    def test_backslash_escapes_from_a_macos_drag(self):
        self.assertEqual(settings_file.unquote_path("/Users/sample/My\\ Music", posix=True),
                         "/Users/sample/My Music")

    def test_a_windows_path_keeps_its_backslashes(self):
        self.assertEqual(settings_file.unquote_path("D:\\Music", posix=False), "D:\\Music")

    def test_only_one_matching_pair(self):
        self.assertEqual(settings_file.unquote_path('"half', posix=False), '"half')
        self.assertEqual(settings_file.unquote_path("\"mixed'", posix=False), "\"mixed'")


class TheMusicFolderQuestion(Prompted):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()

    def test_a_quoted_existing_folder_is_taken(self):
        folder = self.answer([], lambda: configure.settle_music_directory(f'"{self.tree.music}"'))
        self.assertEqual(folder, self.tree.music)
        self.assertNotIn("does not exist", self.said)

    def test_a_relative_answer_is_never_offered_for_creating(self):
        """The POSIX half: a non-absolute path was created relative to the
        working directory. Now it is asked for again, and nothing is made."""
        name = "sample-relative-folder-1272"
        self.assertFalse(os.path.exists(name))
        folder = self.answer([""], lambda: configure.settle_music_directory(name))
        self.assertEqual(folder, "")
        self.assertIn("not a full path", self.said)
        self.assertNotIn("Create it now?", self.said)
        self.assertFalse(os.path.exists(name))

    def test_a_missing_full_path_can_still_be_created(self):
        wanted = os.path.join(self.tree.root, "new-music")
        folder = self.answer(["y"], lambda: configure.settle_music_directory(f'"{wanted}"'))
        self.assertEqual(folder, wanted)
        self.assertTrue(os.path.isdir(wanted))

    def test_collect_answers_uses_it(self):
        self.addCleanup(setattr, adminchat, "_read_password", adminchat._read_password)
        adminchat._read_password = lambda prompt: "sample-secret"
        changes, _hash = self.answer(
            ["SampleBot", "", "#example-room", "SampleAdmin", "",
             f'"{self.tree.music}"', "n"],
            configure.collect_answers)
        self.assertEqual(changes["FILE_DIRECTORY"], self.tree.music)


class TheChannelQuestion(Prompted):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.addCleanup(setattr, adminchat, "_read_password", adminchat._read_password)
        adminchat._read_password = lambda prompt: "sample-secret"

    def run_with_channels(self, *channel_answers):
        return self.answer(
            ["SampleBot", "", *channel_answers, "SampleAdmin", "", self.tree.music, "n"],
            configure.collect_answers)

    def test_a_channel_without_its_hash_is_asked_again(self):
        changes, _hash = self.run_with_channels("example-room", "#example-room")
        self.assertEqual(changes["CHANNEL"], "#example-room")
        self.assertIn("does not start with #", self.said)

    def test_channels_separated_by_spaces_are_asked_again(self):
        changes, _hash = self.run_with_channels("#example-room #other-room",
                                                "#example-room,#other-room")
        self.assertEqual(changes["CHANNEL"], "#example-room,#other-room")


if __name__ == "__main__":
    unittest.main()
