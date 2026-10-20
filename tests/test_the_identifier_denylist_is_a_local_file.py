"""The identifier denylist is a local file, and CI writes its own copy.

tests/forbidden_identifiers.py is read by tests/test_no_personal_identifiers_ship.py.
It is not tracked: .gitignore names it, each maintainer keeps a local copy,
and CI writes one from the FORBIDDEN_IDENTIFIERS secret in a step of
.github/workflows/tests.yml. Checked here by running each part for real:

  * git does not track it and ignores it;
  * with no copy the guard's denylist test skips, saying the sweep did not
    run, and fails nothing;
  * on GitHub Actions a hit is reported by file:line alone, without the
    entry's label;
  * the CI step, run with bash on planted values: what it writes, what it
    refuses, where a run without the secret fails (REQUIRE_IDENTIFIER_SWEEP
    set, not a pull request from a fork), and that its output carries
    neither the secret nor a line of what it decodes.

Every value here is a planted placeholder. scripts/preflight.py's note about
an absent copy is tested in tests/test_preflight_counts_what_was_skipped.py,
next to the skip report it prints under.
"""

import base64
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# The module, not its classes: a TestCase imported by name here would be
# collected and run a second time as one of this file's tests.
import tests.test_no_personal_identifiers_ship as guard  # noqa: E402
from tests.support import temp_dir  # noqa: E402
from tests.test_the_launchers_find_the_conf_dir import bash_runs_in_a_folder  # noqa: E402

# The guard's own path for it, not a copy kept here.
DENYLIST = guard.DENYLIST
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
SECRET = "FORBIDDEN_IDENTIFIERS"
REQUIRE = "REQUIRE_IDENTIFIER_SWEEP"
FORK = "FROM_A_FORK"

# The step's whole env: the secret, the repository variable that makes a run
# without it fail, and whether the run is a pull request from a fork.
STEP_ENV = {
    SECRET: "${{ secrets.%s }}" % SECRET,
    REQUIRE: "${{ vars.%s }}" % REQUIRE,
    FORK: ("${{ github.event_name == 'pull_request' && "
           "github.event.pull_request.head.repo.full_name != github.repository }}"),
}

HAVE_BASH = bash_runs_in_a_folder()
NO_BASH = ("no bash here that can run in a folder (Windows without Git Bash); "
           "the Linux and macOS runners run this")


def git_env():
    """os.environ without the caller's GIT_* variables: run from a git hook,
    GIT_DIR and GIT_INDEX_FILE would point every command here at another
    repository."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(args, cwd, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env or git_env(),
                          capture_output=True, timeout=120)


def this_checkout_is_a_git_work_tree():
    """Whether git answers for REPO_ROOT itself - not for some repository
    above it, which is what a tree extracted into a folder of one would get."""
    try:
        top = git(["rev-parse", "--show-toplevel"], REPO_ROOT)
    except (OSError, subprocess.SubprocessError):
        return False
    if top.returncode != 0:
        return False
    here = os.path.normcase(os.path.realpath(REPO_ROOT))
    there = os.path.normcase(os.path.realpath(top.stdout.decode("utf-8", "replace").strip()))
    return here == there


class GitDoesNotCarryIt(unittest.TestCase):
    """The INDEX, not HEAD: the index is what the next commit is made from,
    so it is right before the commit that stops tracking the file and after
    it alike, where HEAD would still list the file until then."""

    @classmethod
    def setUpClass(cls):
        if not this_checkout_is_a_git_work_tree():
            raise unittest.SkipTest("this tree is not a git work tree of its own, so "
                                    "there is no index to ask")

    def test_it_is_not_in_the_index(self):
        listed = git(["ls-files", "-z", "--", DENYLIST], REPO_ROOT)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout, b"", f"{DENYLIST} is tracked")

    def test_gitignore_names_it(self):
        """--no-index: the ignore rule is asked about whether or not the
        file happens to be tracked, so a rule that was deleted is caught on
        a checkout that never had the file too."""
        ignored = git(["check-ignore", "-q", "--no-index", "--", DENYLIST], REPO_ROOT)
        self.assertEqual(ignored.returncode, 0,
                         f"no .gitignore rule matches {DENYLIST} "
                         f"({ignored.stderr.decode('utf-8', 'replace').strip()})")

    def test_control_a_tracked_file_is_in_the_index(self):
        listed = git(["ls-files", "-z", "--", "tests/test_no_personal_identifiers_ship.py"], REPO_ROOT)
        self.assertNotEqual(listed.stdout, b"")


def load_guard_without_the_denylist():
    """A fresh copy of the guard module, imported as if the denylist were
    absent: sys.modules holding None for a name makes importing it raise
    ImportError, under both names the guard tries."""
    path = os.path.join(REPO_ROOT, "tests", "test_no_personal_identifiers_ship.py")
    spec = importlib.util.spec_from_file_location("guard_without_the_denylist", path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"tests.forbidden_identifiers": None,
                                       "forbidden_identifiers": None}):
        spec.loader.exec_module(module)
    return module


def run_the_sweep(module, texts):
    """The guard's denylist test, run on `texts` ({path: text}) instead of
    the shipped files. The result, with its one test run."""
    case = module.NothingIdentifyingShips("test_no_forbidden_identifier_is_in_the_export")
    case.texts, case.unread, case.reason = texts, [], None
    result = unittest.TestResult()
    case.run(result)
    return result


class AbsentMeansTheSweepSkips(unittest.TestCase):

    def setUp(self):
        self.module = load_guard_without_the_denylist()

    def test_the_denylist_reads_as_none(self):
        self.assertIsNone(self.module.FORBIDDEN)

    def test_the_sweep_skips_and_says_it_did_not_run(self):
        result = run_the_sweep(self.module, {"notes.txt": "nothing to see\n"})

        self.assertEqual((result.failures, result.errors), ([], []))
        self.assertEqual(len(result.skipped), 1)
        reason = result.skipped[0][1]
        self.assertIn("did not run", reason)
        self.assertIn(DENYLIST, reason)

    def test_control_with_a_denylist_the_same_sweep_runs(self):
        """The skip above is the absence, not something else: given a
        denylist, the same copy runs and passes on the same clean text."""
        self.module.FORBIDDEN = {guard.word_hash("placeholderentry"): "a planted label"}
        result = run_the_sweep(self.module, {"notes.txt": "nothing to see\n"})

        self.assertEqual((result.failures, result.errors, result.skipped), ([], [], []))
        self.assertEqual(result.testsRun, 1)


PLANTED_WORD = "plantedplaceholderword"
PLANTED_LABEL = "planted-label-kept-off-public-logs"


class AHitOnAPublicLogHasNoLabel(unittest.TestCase):
    """A planted word in a temp tree, found by the guard's own test, with
    GITHUB_ACTIONS set and unset."""

    def setUp(self):
        root = temp_dir(self, prefix="dccore-denylist-hit-")
        with io.open(os.path.join(root, "notes.txt"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("first line\nthis names " + PLANTED_WORD + " here\n")
        self.texts, unread = guard.read_text_files(["notes.txt"], root)
        self.assertEqual(unread, [])
        self.addCleanup(setattr, guard, "FORBIDDEN", guard.FORBIDDEN)
        guard.FORBIDDEN = {guard.word_hash(PLANTED_WORD): PLANTED_LABEL}

    def failure(self, environ):
        with mock.patch.dict(os.environ, environ):
            if "GITHUB_ACTIONS" not in environ:
                os.environ.pop("GITHUB_ACTIONS", None)
            result = run_the_sweep(guard, self.texts)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.failures), 1, "the planted word was not found")
        return result.failures[0][1]

    def test_on_github_actions_only_the_place_is_printed(self):
        said = self.failure({"GITHUB_ACTIONS": "true"})

        self.assertIn("notes.txt:2", said)
        self.assertIn("a listed identifier", said)
        self.assertNotIn(PLANTED_LABEL, said)

    def test_elsewhere_the_label_is_printed_beside_the_place(self):
        said = self.failure({})

        self.assertIn("notes.txt:2", said)
        self.assertIn(PLANTED_LABEL, said)

    def test_not_true_is_not_github_actions(self):
        said = self.failure({"GITHUB_ACTIONS": "false"})

        self.assertIn(PLANTED_LABEL, said)

    def test_on_github_actions_hits_under_two_labels_are_one_list(self):
        """Not grouped by entry: how many distinct entries were hit is not
        printed either."""
        guard.FORBIDDEN[guard.word_hash("secondplantedword")] = "second-planted-label"
        self.texts["more.txt"] = "x\ny\nsecondplantedword\n"
        said = self.failure({"GITHUB_ACTIONS": "true"})

        self.assertNotIn("second-planted-label", said)
        self.assertNotIn(PLANTED_LABEL, said)
        self.assertLess(said.index("more.txt:3"), said.index("notes.txt:2"))


# ---------------------------------------------------------------------------
# The workflow step that writes the denylist.

SECRET_REFERENCE = re.compile(r"secrets\s*(?:\.\s*" + SECRET + r"\b|\[\s*['\"]" + SECRET + r"['\"]\s*\])")


def read_workflow():
    with io.open(WORKFLOW, encoding="utf-8") as handle:
        return handle.read()


def indent(line):
    return len(line) - len(line.lstrip(" "))


def steps_of(text):
    """[(first line number, [lines])] for every step of the workflow's steps
    list, sliced by indentation: a step starts at a `- ` item at the list's
    indent and runs until the next one or anything indented less."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "steps:")
    item = None
    steps = []
    for number in range(start + 1, len(lines)):
        line = lines[number]
        # Blank lines and comments go with the step they sit in.
        if not line.strip() or (line.lstrip().startswith("#") and item is not None):
            if steps:
                steps[-1][1].append(line)
            continue
        if item is None:
            item = indent(line)
        if indent(line) < item:
            break
        if indent(line) == item and line.lstrip().startswith("- "):
            steps.append((number, [line]))
        elif steps:
            steps[-1][1].append(line)
    return steps


def keys_of(step_lines):
    """{key: (value on the key's line, [the lines under it])} for one step."""
    first = step_lines[0]
    key_indent = indent(first) + 2
    lines = [" " * key_indent + first.lstrip()[2:]] + step_lines[1:]
    keys, current = {}, None
    for line in lines:
        if line.strip() and indent(line) == key_indent and not line.lstrip().startswith("#"):
            key, _, value = line.strip().partition(":")
            current = key
            keys[key] = (value.strip(), [])
        elif current is not None:
            keys[current][1].append(line)
    return keys


def block_scalar(lines):
    """A `|` block's text: its lines dedented by the first one's indent, one
    trailing newline kept - what YAML makes of it."""
    body = list(lines)
    while body and not body[-1].strip():
        body.pop()
    cut = indent(next(line for line in body if line.strip()))
    return "\n".join(line[cut:] for line in body) + "\n"


def denylist_step():
    """(index among the steps, its keys, the run text) for the one step whose
    env takes the secret."""
    steps = steps_of(read_workflow())
    found = [(index, keys_of(lines)) for index, (_n, lines) in enumerate(steps)
             if SECRET_REFERENCE.search("\n".join(lines))]
    if len(found) != 1:
        raise AssertionError(f"{len(found)} steps reference the {SECRET} secret, not one")
    index, keys = found[0]
    value, lines = keys["run"]
    if value != "|":
        raise AssertionError(f"the step's run is not a `|` block (it is {value!r})")
    return index, keys, block_scalar(lines)


class TheStepIsWhereItMustBe(unittest.TestCase):

    def test_it_runs_before_the_regression_suite(self):
        index, _keys, _run = denylist_step()
        steps = [keys_of(lines) for _n, lines in steps_of(read_workflow())]
        suite = [i for i, keys in enumerate(steps)
                 if "run_tests_in_parallel.py" in keys.get("run", ("", []))[0]
                 + "\n".join(keys.get("run", ("", []))[1])]
        self.assertTrue(suite, "control: a step runs the regression suite")
        self.assertLess(index, min(suite))

    def test_it_runs_under_bash(self):
        _index, keys, _run = denylist_step()
        self.assertEqual(keys.get("shell", ("", []))[0], "bash")

    def test_the_secret_is_referenced_only_under_env(self):
        """Never in `run:`, where an expression is pasted into the script
        before the shell sees it; anywhere else in the file at all would be
        a second, unchecked route."""
        text = read_workflow()
        lines = text.splitlines()
        steps = steps_of(text)
        index, keys, run = denylist_step()
        first, step_lines = steps[index]
        env_value, env_lines = keys["env"]
        self.assertEqual(env_value, "")
        # The env block's own lines, by their numbers in the file.
        env_start = first + step_lines.index(next(l for l in step_lines if l.strip() == "env:"))
        env_numbers = set(range(env_start + 1, env_start + 1 + len(env_lines)))
        mentioned = {number for number, line in enumerate(lines) if SECRET_REFERENCE.search(line)}
        self.assertTrue(mentioned, "control: the secret is referenced")
        self.assertEqual(sorted(mentioned - env_numbers), [])
        self.assertNotRegex(run, SECRET_REFERENCE)
        self.assertNotIn("${{", run)
        self.assertRegex("\n".join(env_lines), re.compile(
            r"^\s*" + SECRET + r":\s*\$\{\{\s*secrets\." + SECRET + r"\s*\}\}\s*$", re.M))

    def test_its_env_is_the_secret_the_variable_and_the_fork_test(self):
        """All of it, read from the slice: a fourth variable, or one of the
        three changed, is a step these tests no longer describe."""
        _index, keys, _run = denylist_step()
        env = {}
        for line in keys["env"][1]:
            if line.strip() and not line.lstrip().startswith("#"):
                name, _, value = line.strip().partition(":")
                env[name] = value.strip()
        self.assertEqual(env, STEP_ENV)

    def test_the_slice_agrees_with_a_yaml_parser(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed here; the slice above is what the other checks read")
        parsed = yaml.safe_load(read_workflow())
        (job,) = parsed["jobs"].values()
        index, keys, run = denylist_step()
        step = job["steps"][index]
        self.assertEqual(step["run"], run)
        self.assertEqual(step["shell"], "bash")
        self.assertEqual(step["env"], STEP_ENV)
        self.assertEqual(step["name"], keys["name"][0])


def b64(data):
    return base64.b64encode(data).decode("ascii")


VALID = b'FORBIDDEN = {"0123456789abcdef": "a planted label"}\n'

# A value planted in REQUIRE_IDENTIFIER_SWEEP: the step never prints it.
PLANTED_REQUIRE = "zzplantedrequirevaluezz"


@unittest.skipUnless(HAVE_BASH, NO_BASH)
class TheStepRunsOnPlantedValues(unittest.TestCase):
    """The step's own `run:` text, run the way GitHub runs a `shell: bash`
    step, in a temp folder laid out like a checkout's tests/."""

    @classmethod
    def setUpClass(cls):
        _index, _keys, cls.run_text = denylist_step()

    def setUp(self):
        self.folder = temp_dir(self, prefix="dccore-denylist-step-")
        os.makedirs(os.path.join(self.folder, "tests"))
        open(os.path.join(self.folder, "tests", "__init__.py"), "w").close()
        self.written = os.path.join(self.folder, *DENYLIST.split("/"))

    def step(self, secret=None, require=None, fork=None):
        """(exit code, stdout + stderr) of the step with `secret`, `require`
        (REQUIRE_IDENTIFIER_SWEEP) and `fork` (FROM_A_FORK) in the
        environment - each left out entirely for None. Whatever this host
        has under those names is dropped first, so it cannot flip a result."""
        env = {k: v for k, v in os.environ.items() if k not in (SECRET, REQUIRE, FORK)}
        if require is not None:
            env[REQUIRE] = require
        if fork is not None:
            env[FORK] = fork
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # The step calls `python`; this interpreter answers to that name,
        # whatever the host's PATH holds under it.
        env["DCCORE_TEST_PYTHON"] = sys.executable.replace("\\", "/")
        if secret is not None:
            env[SECRET] = secret
        script = 'python() { "$DCCORE_TEST_PYTHON" "$@"; }\n' + self.run_text
        done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                              cwd=self.folder, env=env, capture_output=True, timeout=120)
        said = (done.stdout + done.stderr).decode("utf-8", "replace")
        if secret and secret.strip():
            self.assertNotIn(secret.strip(), said, "the secret's value is in the step's output")
        return done.returncode, said

    def written_bytes(self):
        with open(self.written, "rb") as handle:
            return handle.read()

    def test_no_secret_does_nothing_and_passes(self):
        for secret in (None, "", "  \n "):
            with self.subTest(secret=secret):
                code, said = self.step(secret)
                self.assertEqual(code, 0, said)
                self.assertFalse(os.path.exists(self.written))
                self.assertIn("will skip", said)

    def test_a_valid_secret_is_written_as_it_was_encoded(self):
        code, said = self.step(b64(VALID))

        self.assertEqual(code, 0, said)
        self.assertEqual(self.written_bytes(), VALID)
        self.assertNotIn("a planted label", said)

    def test_base64_wrapped_over_lines_is_the_same_file(self):
        """What `base64` without -w0 prints, CRLF included."""
        for wrapped in (base64.encodebytes(VALID * 3).decode("ascii"),
                        base64.encodebytes(VALID * 3).decode("ascii").replace("\n", "\r\n")):
            with self.subTest(newline=repr(wrapped[-2:])):
                self.assertIn("\n", wrapped.strip(), "control: it is wrapped")
                code, said = self.step(wrapped)
                self.assertEqual(code, 0, said)
                self.assertEqual(self.written_bytes(), VALID * 3)

    def test_what_is_not_base64_fails_naming_the_error_type_only(self):
        """Including valid base64 with a stray character in it: a lenient
        decode would drop the character and write a file anyway."""
        encoded = b64(VALID)
        for secret in ("this is not base64!", encoded[:20] + "*" + encoded[20:]):
            with self.subTest(secret=secret[:8]):
                code, said = self.step(secret)
                self.assertNotEqual(code, 0, said)
                self.assertIn("not valid base64 (Error)", said)
                self.assertFalse(os.path.exists(self.written))

    def test_a_file_that_does_not_import_fails_without_quoting_it(self):
        planted = "zz-planted-secret-looking-line-zz"
        cases = {
            "SyntaxError": f'PLANTED = "{planted}"\nFORBIDDEN = {{\n'.encode("ascii"),
            # An error whose own message quotes the file: only its type may
            # reach the log.
            "NameError": b"FORBIDDEN = {zzplantedsecretname: 1}\n",
        }
        for kind, content in cases.items():
            with self.subTest(kind=kind):
                code, said = self.step(b64(content))
                self.assertNotEqual(code, 0, said)
                self.assertIn(f"does not import ({kind})", said)
                self.assertNotIn(planted, said)
                self.assertNotIn("zzplantedsecretname", said)
                for line in content.decode("ascii").splitlines():
                    self.assertNotIn(line, said)

    def test_a_file_with_no_entries_fails(self):
        for content in (b"FORBIDDEN = {}\n", b"FORBIDDEN = ['not', 'a', 'dict']\n"):
            with self.subTest(content=content):
                code, said = self.step(b64(content))
                self.assertNotEqual(code, 0, said)
                self.assertIn("holds no FORBIDDEN entries", said)

    # REQUIRE_IDENTIFIER_SWEEP and FROM_A_FORK: a run without the secret
    # fails where the repository says the sweep must run, unless it is a pull
    # request from a fork, which GitHub gives no secrets at all.

    def test_without_the_secret_only_a_required_run_not_from_a_fork_fails(self):
        """The variable x fork matrix, for no secret, an empty one and a
        blank one. It fails exactly where the variable is not blank and
        FROM_A_FORK is not "true" - missing included: that fails closed."""
        for secret in (None, "", " \n "):
            for require in (None, "", "  \t ", "1", PLANTED_REQUIRE):
                for fork in (None, "true", "false", "True"):
                    with self.subTest(secret=secret, require=require, fork=fork):
                        code, said = self.step(secret, require, fork)
                        must_fail = bool(require and require.strip()) and fork != "true"
                        self.assertFalse(os.path.exists(self.written))
                        self.assertNotIn(PLANTED_REQUIRE, said)
                        if must_fail:
                            self.assertNotEqual(code, 0, said)
                            self.assertIn("the identifier sweep must run here", said)
                            self.assertNotIn("will skip", said)
                        else:
                            self.assertEqual(code, 0, said)
                            self.assertIn("will skip", said)

    def test_with_a_secret_the_variable_and_the_fork_change_nothing(self):
        for require in (None, "1"):
            for fork in (None, "true", "false"):
                with self.subTest(require=require, fork=fork):
                    if os.path.exists(self.written):
                        os.remove(self.written)
                    code, said = self.step(b64(VALID), require, fork)
                    self.assertEqual(code, 0, said)
                    self.assertEqual(self.written_bytes(), VALID)

    def test_a_bad_secret_where_the_sweep_is_required_names_the_error_type_only(self):
        planted = "zzplantedsecretvaluezz!"
        code, said = self.step(planted, "1", "false")

        self.assertNotEqual(code, 0, said)
        self.assertIn("not valid base64 (Error)", said)
        self.assertNotIn("zzplantedsecretvaluezz", said)
        self.assertFalse(os.path.exists(self.written))

    def test_this_hosts_own_values_cannot_flip_a_result(self):
        """step() drops whatever this machine has under the three names."""
        with mock.patch.dict(os.environ, {SECRET: b64(VALID), REQUIRE: "1", FORK: "false"}):
            code, said = self.step()
        self.assertEqual(code, 0, said)
        self.assertIn("will skip", said)
        self.assertFalse(os.path.exists(self.written))

        with mock.patch.dict(os.environ, {FORK: "true"}):
            code, said = self.step(None, "1")
        self.assertNotEqual(code, 0, said)


if __name__ == "__main__":
    unittest.main()
