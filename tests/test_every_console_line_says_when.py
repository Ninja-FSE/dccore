"""Every line the daemon prints carries the time it was printed.

Reported live. Four channels failed to confirm at startup and the operator,
reading the console window, could not tell whether the retry had fired yet -
nothing in the window said when anything had happened. A log line with no
time on it answers "what" and never "when".

The stamp is applied by a stream proxy installed on stdout and stderr at the
same moment as the console-encoding guard, so every print() in every module
gets it without any module changing. These tests drive the proxy directly with
a fake stream, and check the daemon installs it and that the operator's
setting reaches it.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import platform_compat  # noqa: E402

STAMP = re.compile(r"^\[\d\d:\d\d:\d\d\] ", re.MULTILINE)


def wrapped(fmt="%H:%M:%S"):
    buf = io.StringIO()
    return buf, platform_compat._TimestampedStream(buf, lambda: fmt)


class EveryLineIsStamped(unittest.TestCase):

    def test_one_print_one_stamp(self):
        buf, out = wrapped()
        print("hello", file=out)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], STAMP)
        self.assertTrue(lines[0].endswith("] hello"))

    def test_a_multi_line_write_stamps_every_line(self):
        """A traceback or a folder listing arrives as one write. Each line of
        it is a line in the log, and each gets its own time."""
        buf, out = wrapped()
        out.write("first\nsecond\nthird\n")
        for line in buf.getvalue().splitlines():
            with self.subTest(line=line):
                self.assertRegex(line, STAMP)

    def test_a_line_built_from_several_writes_is_stamped_once(self):
        """print() sends the text and the newline separately. That is one line,
        and must not become '[t] text[t] '."""
        buf, out = wrapped()
        out.write("part")
        out.write("ial")
        out.write("\n")
        self.assertEqual(len(STAMP.findall(buf.getvalue())), 1)
        self.assertTrue(buf.getvalue().endswith("] partial\n"))

    def test_the_stamp_goes_at_the_start_of_the_next_line_only(self):
        buf, out = wrapped()
        print("one", file=out)
        print("two", file=out)
        text = buf.getvalue()
        self.assertEqual(text.count("["), 2)
        self.assertNotIn("]\n", text, "a stamp with nothing after it is a blank stamped line")

    def test_an_empty_write_writes_nothing(self):
        buf, out = wrapped()
        out.write("")
        self.assertEqual(buf.getvalue(), "")

    def test_the_return_value_is_what_the_caller_wrote(self):
        """A caller comparing the return to len(text) must not be told its
        write grew by a stamp."""
        buf, out = wrapped()
        self.assertEqual(out.write("abc\n"), 4)

    def test_writelines_goes_through_the_same_path(self):
        buf, out = wrapped()
        out.writelines(["a\n", "b\n"])
        self.assertEqual(len(STAMP.findall(buf.getvalue())), 2)


class TheFormatIsTheOperators(unittest.TestCase):

    def test_an_empty_format_means_no_prefix_at_all(self):
        buf, out = wrapped(fmt="")
        print("plain", file=out)
        self.assertEqual(buf.getvalue(), "plain\n")

    def test_a_date_format_is_honoured(self):
        buf, out = wrapped(fmt="%Y-%m-%d %H:%M:%S")
        print("x", file=out)
        self.assertRegex(buf.getvalue(), r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] x\n$")

    def test_the_format_is_read_per_line_so_it_can_change_after_install(self):
        """The daemon installs the wrapper before config has loaded, then
        applies the operator's format. Captured at install, the setting would
        never take effect."""
        current = {"fmt": "%H:%M:%S"}
        buf = io.StringIO()
        out = platform_compat._TimestampedStream(buf, lambda: current["fmt"])
        print("a", file=out)
        current["fmt"] = ""
        print("b", file=out)
        lines = buf.getvalue().splitlines()
        self.assertRegex(lines[0], STAMP)
        self.assertEqual(lines[1], "b")

    def _assert_refused(self, bad):
        before = platform_compat.console_timestamp_format()
        try:
            platform_compat.set_console_timestamp_format("%H:%M:%S")
            kept = io.StringIO()
            real = sys.stdout
            sys.stdout = kept
            try:
                result = platform_compat.set_console_timestamp_format(bad)
            finally:
                sys.stdout = real
            self.assertEqual(result, "%H:%M:%S")
            self.assertEqual(platform_compat.console_timestamp_format(), "%H:%M:%S")
            self.assertIn("not a valid", kept.getvalue())
        finally:
            platform_compat.set_console_timestamp_format(before)

    def test_a_format_strftime_rejects_is_refused_and_the_old_one_kept(self):
        """A typo must not become a ValueError inside every print() for the
        life of the process - the encoding guard exists to stop exactly that
        class of failure.

        The refusal is "whatever THIS platform's strftime raises on", because
        that is where the hazard is: Windows' C runtime raises ValueError on
        an unknown directive, glibc passes it through as literal text, which
        is harmless and nothing to refuse. So this drives the refusal path
        with strftime made to raise, and runs everywhere; the probe below
        checks the same thing against the real strftime where it applies.
        """
        real_strftime = platform_compat.time.strftime

        def strict(fmt, *rest):
            if "%Q" in fmt:
                raise ValueError("Invalid format string")
            return real_strftime(fmt, *rest)

        platform_compat.time.strftime = strict
        try:
            self._assert_refused("%Q %Z %")
        finally:
            platform_compat.time.strftime = real_strftime

    def test_a_format_this_platform_really_rejects_is_refused(self):
        """The same property against the real strftime, where it has one."""
        import time
        try:
            time.strftime("%Q %Z %")
        except ValueError:
            pass
        else:
            self.skipTest("this platform's strftime passes unknown directives "
                          "through as text; there is nothing for it to refuse")
        self._assert_refused("%Q %Z %")

    def test_a_format_strftime_accepts_is_kept_verbatim(self):
        """The other half: glibc-style pass-through must not be mistaken for
        invalid. What strftime accepts, we accept."""
        before = platform_compat.console_timestamp_format()
        try:
            self.assertEqual(platform_compat.set_console_timestamp_format("%Y-%m-%d %H:%M:%S"),
                             "%Y-%m-%d %H:%M:%S")
        finally:
            platform_compat.set_console_timestamp_format(before)

    def test_none_reads_as_off(self):
        before = platform_compat.console_timestamp_format()
        try:
            self.assertEqual(platform_compat.set_console_timestamp_format(None), "")
        finally:
            platform_compat.set_console_timestamp_format(before)


class TheRealStreamIsStillThere(unittest.TestCase):
    """Code that inspects sys.stdout must find what it always found."""

    def test_attributes_are_delegated(self):
        buf, out = wrapped()
        self.assertIs(out.wrapped, buf)
        self.assertEqual(out.isatty(), buf.isatty())
        self.assertEqual(out.getvalue(), buf.getvalue())

    def test_reconfigure_reaches_the_real_stream(self):
        """The encoding guard calls reconfigure(); on a wrapped stream that call
        has to land on the real TextIOWrapper or the guard silently stops
        guarding."""
        raw = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        out = platform_compat._TimestampedStream(raw, lambda: "")
        out.reconfigure(encoding="utf-8", errors="replace")
        self.assertEqual(raw.encoding, "utf-8")

    def test_the_encoding_guard_still_works_on_a_wrapped_stream(self):
        raw = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        out = platform_compat._TimestampedStream(raw, lambda: "")
        changed = platform_compat.install_console_encoding_guard(
            streams=[("stdout", out)])
        self.assertEqual(changed, ["stdout"])
        self.assertEqual(raw.encoding, "utf-8")


class ItRejectsBytesLikeARealTextStreamWould(unittest.TestCase):
    """Took the live dashboard down within seconds of shipping.

    Flask's CLI banner prints through click.echo(), and click decides
    whether a stream takes str or bytes by probing it: stream.write(b"").
    The proxy's write() answered `if not text: return 0` for ANY falsy
    argument, string or not - so the probe "succeeded" without ever
    reaching code that would notice b"" is not a str. click concluded this
    was a binary stream, wrapped it in its own encoder, and started feeding
    every later write here as encoded bytes - which then failed inside
    write()'s own line-splitting on the first real line printed after the
    dashboard started, taking it down immediately (`[WEBUI] Dashboard
    stopped: a bytes-like object is required, not 'str'`).

    A real text-mode stream raises TypeError for stream.write(b"") too, so
    matching that contract exactly is both the fix and the property to
    pin - not "handle bytes gracefully", which would have made the proxy
    correct in a way click's probe still cannot see.
    """

    def test_empty_bytes_is_refused_not_silently_accepted(self):
        """The exact probe click.utils._is_binary_writer() makes. Answering
        it with success (the old `if not text: return 0`, true for b"" as
        much as for "") is the entire bug."""
        _, out = wrapped()

        with self.assertRaises(TypeError):
            out.write(b"")

    def test_non_empty_bytes_is_refused_too(self):
        _, out = wrapped()

        with self.assertRaises(TypeError):
            out.write(b"hello\n")

    def test_a_real_text_stream_agrees(self):
        """Control: the proxy's behaviour matches what it is standing in
        for, not a rule invented for this fix."""
        real = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

        with self.assertRaises(TypeError):
            real.write(b"")

    def test_an_empty_string_is_still_accepted(self):
        """The other half - the fix must not turn every no-op write into an
        error along with the bytes case it was meant to catch."""
        _, out = wrapped()

        self.assertEqual(out.write(""), 0)

    def test_clicks_own_probe_now_says_text(self):
        """Not a reimplementation of the contract above - the actual
        function that misjudged this proxy live, run against it directly.
        Skips where click is not installed (Flask is optional); this is
        exactly the dependency whose CLI banner triggered the incident."""
        try:
            from click._compat import _is_binary_writer
        except ImportError:
            self.skipTest("click is not installed")

        _, out = wrapped()

        self.assertFalse(_is_binary_writer(out, False))


class InstallingIt(unittest.TestCase):

    def setUp(self):
        self._out, self._err = sys.stdout, sys.stderr
        self._fmt = platform_compat.console_timestamp_format()

    def tearDown(self):
        sys.stdout, sys.stderr = self._out, self._err
        platform_compat.set_console_timestamp_format(self._fmt)

    def test_wraps_both_streams_and_says_so(self):
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        changed = platform_compat.install_console_timestamps()
        self.assertEqual(changed, ["stdout", "stderr"])
        self.assertIsInstance(sys.stdout, platform_compat._TimestampedStream)
        self.assertIsInstance(sys.stderr, platform_compat._TimestampedStream)

    def test_installing_twice_does_not_double_wrap(self):
        """Idempotent, or a second entry point would stamp every line twice."""
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        platform_compat.install_console_timestamps()
        inner = sys.stdout.wrapped
        self.assertEqual(platform_compat.install_console_timestamps(), [])
        self.assertIs(sys.stdout.wrapped, inner)

    def test_a_missing_stream_is_skipped(self):
        """pythonw.exe gives None for both."""
        sys.stdout, sys.stderr = None, io.StringIO()
        self.assertEqual(platform_compat.install_console_timestamps(), ["stderr"])
        self.assertIsNone(sys.stdout)

    def test_the_installed_wrapper_stamps_a_print(self):
        buf = io.StringIO()
        sys.stdout = buf
        sys.stderr = io.StringIO()
        platform_compat.install_console_timestamps()
        print("live")
        self.assertRegex(buf.getvalue(), STAMP)


class TheDaemonInstallsIt(unittest.TestCase):

    def setUp(self):
        with io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_installed_with_the_encoding_guard(self):
        self.assertIn("platform_compat.install_console_timestamps()", self.source)

    def test_installed_before_config_loads(self):
        """So the config-loading lines - the first two the operator sees - are
        stamped too, rather than the stamps starting three lines in."""
        installed = self.source.index("platform_compat.install_console_timestamps()")
        config_import = self.source.index("import defaults as config")
        self.assertLess(installed, config_import)

    def test_the_operators_format_is_applied_after_config_loads(self):
        applied = self.source.index("set_console_timestamp_format(")
        config_import = self.source.index("import defaults as config")
        self.assertGreater(applied, config_import)
        self.assertIn('"CONSOLE_TIMESTAMP_FORMAT"', self.source)

    def test_the_reports_do_not_install_it(self):
        """setup_check.py and configure.py print a report for a person to read
        once, not a log; a stamp on every line of a report is noise."""
        for name in ("scripts/setup_check.py", "configure.py"):
            with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                with self.subTest(script=name):
                    self.assertNotIn("install_console_timestamps", handle.read())


class TheSettingIsWiredThrough(unittest.TestCase):

    def test_declared_with_the_mirc_default(self):
        import defaults
        self.assertEqual(defaults.CONSOLE_TIMESTAMP_FORMAT, "%H:%M:%S")
        self.assertIs(defaults.__annotations__.get("CONSOLE_TIMESTAMP_FORMAT"), str)

    def test_in_the_sample_file(self):
        with io.open(os.path.join(REPO_ROOT, "settings.conf.sample"), encoding="utf-8") as handle:
            self.assertIn("#CONSOLE_TIMESTAMP_FORMAT = %H:%M:%S", handle.read())

    def test_on_the_dashboard_under_debug_and_logging(self):
        import webserver
        categories = {key: names for key, _label, names in webserver.SETTINGS_CATEGORIES}
        self.assertIn("CONSOLE_TIMESTAMP_FORMAT", categories["debug"])
        self.assertIn("CONSOLE_TIMESTAMP_FORMAT", webserver.SETTINGS_LABELS)


if __name__ == "__main__":
    unittest.main()
