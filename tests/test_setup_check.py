"""One setup check, two launchers.

scripts/linux/check-setup.py and scripts/windows/check-setup.py were the same
file twice - 151 identical lines checking the same ten settings. Two
hand-maintained copies of one piece of knowledge about config.py is the shape
PRESERVE_RUNTIME already was here, and it had already started drifting: the
Linux script explained that another running instance is the usual reason a DCC
port is busy, and the Windows one never got that sentence.

These tests exist to keep it at one copy. The important one is
NeitherLauncherCarriesChecks - re-duplicating the logic makes it fail rather
than quietly restarting the drift.

They deliberately do not re-test what the checks decide. That logic reads the
real config.py and walks the real library; it is exercised by running the
script, which is how #112 justified having no tests for it, and that argument
still holds for the part that is still a side-effecting script.
"""

import ast
import importlib
import io
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import setup_check  # noqa: E402

SHIMS = {
    "linux": os.path.join(SCRIPTS, "linux", "check-setup.py"),
    "windows": os.path.join(SCRIPTS, "windows", "check-setup.py"),
}


def source(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


class ImportingItIsHarmless(unittest.TestCase):
    """The scripts set sys.path and chdir'd at top level, which was fine while
    nothing imported them. This module is meant to be imported."""

    def test_importing_does_not_move_the_working_directory(self):
        """Run from somewhere that is NOT the repo, deliberately.

        Asserting this while already sitting in the repo proves nothing: a
        top-level os.chdir(REPO) is a no-op there, so the check passes whether
        or not the side effect exists. Verified by putting the chdir back and
        watching this fail.
        """
        elsewhere = tempfile.mkdtemp(prefix="dccore-cwd-")
        original = os.getcwd()
        self.addCleanup(shutil.rmtree, elsewhere, True)
        self.addCleanup(os.chdir, original)
        os.chdir(elsewhere)

        importlib.reload(setup_check)

        self.assertEqual(
            os.path.realpath(os.getcwd()), os.path.realpath(elsewhere),
            "importing the module moved the process's working directory")


class BothPlatformsAreFullyDescribed(unittest.TestCase):

    def platforms(self):
        return {"LINUX": setup_check.LINUX, "WINDOWS": setup_check.WINDOWS}

    def test_every_field_is_set_on_both(self):
        """A missing field would surface as an empty string in the operator's
        report, or as an AttributeError halfway through a check."""
        for name, platform in self.platforms().items():
            for field, value in vars(platform).items():
                self.assertTrue(str(value).strip(),
                                f"{name}.{field} is empty")

    def test_the_os_names_are_the_two_real_ones(self):
        """os.name is what decides whether the "wrong platform" warning fires,
        so a typo here would warn on the correct machine and stay quiet on the
        wrong one."""
        self.assertEqual(setup_check.LINUX.os_name, "posix")
        self.assertEqual(setup_check.WINDOWS.os_name, "nt")

    def test_the_two_platforms_differ_in_every_field(self):
        """Everything in Platform is there BECAUSE it differs. A field that
        happens to match on both is one that did not need to be platform
        specific, and belongs in the shared body instead."""
        same = [field for field, value in vars(setup_check.LINUX).items()
                if getattr(setup_check.WINDOWS, field) == value]

        self.assertEqual(same, [],
                         "these Platform fields are identical on both, so they "
                         "are not platform-specific: " + ", ".join(same))

    def test_each_launcher_points_at_the_other_one(self):
        """The wrong-platform warning tells the operator where to go. It named
        `windows\\check-setup.py` until #113 moved the scripts, and nothing
        noticed."""
        for platform, expected in ((setup_check.LINUX, "scripts/linux/check-setup.py"),
                                   (setup_check.WINDOWS, "scripts\\windows\\check-setup.py")):
            other = (setup_check.WINDOWS if platform is setup_check.LINUX
                     else setup_check.LINUX)
            self.assertIn(expected, other.wrong_os,
                          f"{other.display}'s warning does not name the real "
                          f"path of the other check")

    def test_the_start_commands_name_scripts_that_exist(self):
        for platform in self.platforms().values():
            relative = platform.start_cmd.lstrip("./").replace("\\", "/")
            self.assertTrue(
                os.path.exists(os.path.join(REPO_ROOT, relative)),
                f"{platform.display} tells the operator to run {relative}, "
                f"which is not in the repository")


class NeitherLauncherCarriesChecks(unittest.TestCase):
    """The regression guard. Re-duplicating the logic into a launcher must
    fail here rather than quietly restarting the drift this removed."""

    # Things only the shared module should ever do.
    FORBIDDEN = ("getattr(config", "DCC_PORT_START", "MAX_DCC_SLOTS",
                 "socket.socket", "def fail", "def warn", "def ok")

    def test_the_launchers_contain_no_check_logic(self):
        offenders = []
        for name, path in SHIMS.items():
            body = source(path)
            for needle in self.FORBIDDEN:
                if needle in body:
                    offenders.append(f"{name}: {needle!r}")

        self.assertEqual(
            offenders, [],
            "a launcher has grown check logic of its own; it belongs in "
            "scripts/setup_check.py so both platforms get it: "
            + ", ".join(offenders))

    def test_each_launcher_is_a_shim_and_stays_small(self):
        """Not a style rule - a launcher long enough to hold a check is a
        launcher that will."""
        for name, path in SHIMS.items():
            lines = [l for l in source(path).split("\n") if l.strip()]
            self.assertLess(len(lines), 25,
                            f"{name}'s launcher has {len(lines)} code lines; "
                            f"it should only name its platform")

    def test_each_launcher_calls_main_with_its_own_platform(self):
        expected = {"linux": "LINUX", "windows": "WINDOWS"}
        for name, path in SHIMS.items():
            tree = ast.parse(source(path))
            names = {node.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Attribute)}
            self.assertIn(expected[name], names,
                          f"{name}'s launcher does not pass "
                          f"setup_check.{expected[name]}")
            self.assertIn("main", names, f"{name}'s launcher never calls main()")

    def test_the_checks_live_in_exactly_one_place(self):
        """DCC_PORT_START is the marker: it is the knowledge that was
        duplicated, so counting where it appears counts the copies."""
        holders = []
        for root, _dirs, files in os.walk(SCRIPTS):
            for filename in files:
                if filename.endswith(".py") and "DCC_PORT_START" in source(
                        os.path.join(root, filename)):
                    holders.append(os.path.relpath(
                        os.path.join(root, filename), REPO_ROOT))

        self.assertEqual(len(holders), 1,
                         "the setup check exists in more than one "
                         "file again: " + ", ".join(sorted(holders)))


if __name__ == "__main__":
    unittest.main()
