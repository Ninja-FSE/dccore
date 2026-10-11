"""#1272: CONSOLE_TIMESTAMP_FORMAT changed on the Settings page did nothing
until a restart, and the page did not say so.

set_console_timestamp_format() was called once, in oserve's __main__ block,
and nothing in the rehash path called it again. The neighbouring
CONSOLE_LOG_FILE is read per line and takes effect at once; the stamp now
does the same - read from the live config on every line, validated, and the
previous format kept when a new one is refused.
"""

import ast
import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import platform_compat  # noqa: E402

from tests.support import DCCoreTestCase, parse_source  # noqa: E402


class TheFormatIsReadPerLine(unittest.TestCase):

    def setUp(self):
        before = platform_compat.console_timestamp_format()
        self.addCleanup(platform_compat.set_console_timestamp_format, before)
        self.addCleanup(platform_compat.follow_console_timestamp_format, None)
        self.setting = {"value": "%H:%M:%S"}
        platform_compat.follow_console_timestamp_format(lambda: self.setting["value"])

    def current(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            fmt = platform_compat.console_timestamp_format()
        return fmt, buffer.getvalue()

    def test_a_changed_setting_is_the_next_lines_format(self):
        self.assertEqual(self.current()[0], "%H:%M:%S")
        self.setting["value"] = "%Y-%m-%d %H:%M:%S"
        self.assertEqual(self.current()[0], "%Y-%m-%d %H:%M:%S")

    def test_blank_turns_it_off_live(self):
        self.current()
        self.setting["value"] = ""
        self.assertEqual(self.current()[0], "")

    def test_a_refused_format_keeps_the_previous_one_and_says_so_once(self):
        """Which formats strftime refuses depends on the C library (Windows
        raises on "%Q", glibc prints it), so the refusal is a stand-in
        strftime that refuses one known format, on every platform."""
        real_time = platform_compat.time

        class Refusing:
            def __getattr__(self, name):
                return getattr(real_time, name)

            @staticmethod
            def strftime(fmt, *args):
                if fmt == "REFUSED":
                    raise ValueError("Invalid format string")
                return real_time.strftime(fmt, *args)

        self.addCleanup(setattr, platform_compat, "time", real_time)
        platform_compat.time = Refusing()
        self.current()
        self.setting["value"] = "REFUSED"
        fmt, said = self.current()
        self.assertEqual(fmt, "%H:%M:%S")
        self.assertIn("not a valid", said)
        fmt, said_again = self.current()
        self.assertEqual(fmt, "%H:%M:%S")
        self.assertEqual(said_again, "", "the refusal was repeated on every line")

    def test_a_wrapped_stream_stamps_with_the_new_format(self):
        target = io.StringIO()
        stream = platform_compat._TimestampedStream(target, platform_compat.console_timestamp_format)
        self.setting["value"] = "STAMP-ONE"
        stream.write("first\n")
        self.setting["value"] = "STAMP-TWO"
        stream.write("second\n")
        self.assertEqual(target.getvalue(), "[STAMP-ONE] first\n[STAMP-TWO] second\n")


class TheDaemonFollowsTheLiveConfig(DCCoreTestCase):

    def test_the_source_reads_the_reloaded_module(self):
        from tests.test_startup import real_oserve
        oserve = real_oserve()
        self.set_config(CONSOLE_TIMESTAMP_FORMAT="%d %H:%M")
        self.assertEqual(oserve.current_console_timestamp_format(), "%d %H:%M")
        self.set_config(CONSOLE_TIMESTAMP_FORMAT="")
        self.assertEqual(oserve.current_console_timestamp_format(), "")

    def test_the_program_block_installs_it(self):
        """The call itself, inside `if __name__ == "__main__":` - not the
        name in a comment or a docstring."""
        path = os.path.join(REPO_ROOT, "oserve.py")
        with io.open(path, encoding="utf-8") as handle:
            tree = parse_source(handle.read(), path)
        found = False
        for node in tree.body:
            if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "__name__"):
                continue
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "follow_console_timestamp_format"
                        and [getattr(a, "id", None) for a in call.args]
                        == ["current_console_timestamp_format"]):
                    found = True
        self.assertTrue(found, "oserve's program block does not follow the live setting")


if __name__ == "__main__":
    unittest.main()
