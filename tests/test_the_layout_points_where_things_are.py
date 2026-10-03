"""Instructions and the upgrade guide point where things are after the move (#1088, #1089).

#960 moved the daemon modules into src/ and #983 the operator's config into
conf/. Two kinds of text were left pointing at the old places: every
instruction to run `python adminchat.py` to make the password hash (the file
is src/adminchat.py now), and the upgrade guide's backup command, which became
one `cp -r data conf data.backup` - it fails when data.backup does not exist
yet, and before the upgrade that moves it there is no conf/ to copy.
"""

import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests import support  # noqa: F401  (path setup)

# Imported as a module, not by name: its TestCase classes would be collected again.
import tests.test_the_launchers_find_the_conf_dir as launchers  # noqa: E402

REPO_ROOT = support.REPO_ROOT

# Every file an operator reads an instruction in, or that prints one.
INSTRUCTION_FILES = ("conf/admin_config.py.sample", "conf/settings.conf.sample", "src/defaults.py",
                     "scripts/setup_check.py", "src/webserver.py", "configure.py",
                     "docs/ADMIN-CONSOLE.md", "docs/WINDOWS.md", "docs/INSTALL.md", "README.md")


def read(relative):
    with io.open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


class TheHashGenerator(unittest.TestCase):
    def test_it_is_where_the_instructions_say(self):
        text = read("src/adminchat.py")
        self.assertIn('if __name__ == "__main__":', text)
        self.assertFalse(os.path.exists(os.path.join(REPO_ROOT, "adminchat.py")),
                         "a root adminchat.py is back; then the instructions should say so")

    def test_no_instruction_names_the_old_place(self):
        """`python adminchat.py`, `python3 adminchat.py`, `{platform.python} adminchat.py`."""
        old = re.compile(r"(python3?|\{platform\.python\}|py(?: -3)?)\s+adminchat\.py")
        for relative in INSTRUCTION_FILES:
            if not os.path.exists(os.path.join(REPO_ROOT, relative)):
                continue
            with self.subTest(file=relative):
                self.assertEqual(old.findall(read(relative)), [])

    def test_the_new_place_is_named(self):
        for relative in ("conf/admin_config.py.sample", "docs/ADMIN-CONSOLE.md", "scripts/setup_check.py"):
            with self.subTest(file=relative):
                self.assertIn("src/adminchat.py", read(relative))


def backup_block():
    """The commands in the upgrade guide's step 2, as written."""
    text = read("docs/INSTALL.md")
    step = text.index("**2. Back up `data/` and your config.**")
    start = text.index("```bash\n", step) + len("```bash\n")
    return text[start:text.index("```", start)]


@unittest.skipUnless(launchers.bash_runs_in_a_folder(), "no bash that can run in a folder here")
class TheBackupStepForReal(unittest.TestCase):
    def run_in(self, layout):
        tree = tempfile.mkdtemp(prefix="dccore-backup-")
        self.addCleanup(shutil.rmtree, tree, True)
        os.makedirs(os.path.join(tree, "data"))
        with open(os.path.join(tree, "data", "stats.txt"), "w") as handle:
            handle.write("1 2 3\n")
        config = os.path.join(tree, "conf") if layout == "conf" else tree
        os.makedirs(config, exist_ok=True)
        for name in ("settings.conf", "admin_config.py"):
            with open(os.path.join(config, name), "w") as handle:
                handle.write("# " + name + "\n")
        done = subprocess.run(["bash", "-c", backup_block()], cwd=tree, capture_output=True, timeout=60)
        backup = os.path.join(tree, "data.backup")
        return done, sorted(os.listdir(backup)) if os.path.isdir(backup) else None, backup

    def test_before_the_move_the_root_config_is_backed_up(self):
        done, got, backup = self.run_in("root")
        self.assertEqual(got, ["admin_config.py", "data", "settings.conf"], done.stderr)
        self.assertTrue(os.path.isfile(os.path.join(backup, "data", "stats.txt")))

    def test_after_it_the_conf_config_is(self):
        done, got, _backup = self.run_in("conf")
        self.assertEqual(got, ["admin_config.py", "data", "settings.conf"], done.stderr)


class TheBackupStepAsWritten(unittest.TestCase):
    """Runs everywhere, beside the real run above."""

    def test_it_makes_the_folder_first_and_copies_both_places(self):
        block = backup_block()
        self.assertTrue(block.startswith("mkdir -p data.backup\n"), block)
        self.assertIn("cp settings.conf admin_config.py data.backup/", block)
        self.assertIn("cp conf/settings.conf conf/admin_config.py data.backup/", block)
        self.assertNotIn("cp -r data conf data.backup", block)


if __name__ == "__main__":
    unittest.main()
