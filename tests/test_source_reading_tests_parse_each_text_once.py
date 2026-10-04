"""The source-reading tests parsed the same big modules thousands of times (#1147).

Each one opened src/*.py and ran ast.parse() on it itself: one run made
about 29,000 parses of some 540 distinct texts, irc.py alone opened 210
times, at 50 to 150 ms a parse. tests/support.parse_source() parses a text
once per process and hands every later caller the same tree.

Two things make that safe, and this file holds the suite to both:

* The cache is keyed on the TEXT, not on a path and its mtime. Tests write
  files and scan them again, and a rewrite inside one timestamp tick must
  not hand back the old tree - the trap a stale __pycache__ has already
  sprung on this project once.
* The trees are shared, so nothing may change one. No test module
  subclasses ast.NodeTransformer or calls the helpers that edit a tree.

And every test that parses a text it read goes through parse_source(), so
a new test does not quietly bring the cost back.
"""

import ast
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.support import parse_source, temp_dir  # noqa: E402

TESTS = os.path.join(REPO_ROOT, "tests")
BIG = "".join(f"NAME_{n} = {n}  # a line of padding\n" for n in range(600))   # well over 10,000 characters
TREE_EDITORS = {"fix_missing_locations", "increment_lineno", "copy_location"}
NEEDLES = (".parse(", "NodeTransformer") + tuple(TREE_EDITORS)


def assigned_names(tree):
    return [node.targets[0].id for node in tree.body if isinstance(node, ast.Assign)]


_SCANNED = []


def scanned_test_modules():
    """(name, tree) of every test module that could matter here, read once
    for the three guards below. Parsing all 450 would cost seconds; a module
    whose text holds none of NEEDLES cannot hold a call they look for."""
    if not _SCANNED:
        _SCANNED.extend(_scan())
    return _SCANNED


def _scan():
    for name in sorted(os.listdir(TESTS)):
        if name.endswith(".py") and name not in ("support.py", os.path.basename(__file__)):
            path = os.path.join(TESTS, name)
            with io.open(path, encoding="utf-8") as handle:
                text = handle.read()
            if any(word in text for word in NEEDLES):
                yield name, ast.parse(text, filename=name)


def ast_aliases(tree):
    """The names the ast module is bound to in this module: `import ast`,
    `import ast as _ast`, wherever the import sits."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name for alias in node.names if alias.name == "ast")
    return names


class TheCacheIsKeyedOnTheText(unittest.TestCase):

    def test_the_same_text_is_parsed_once(self):
        self.assertGreater(len(BIG), 10000)
        first = parse_source(BIG)
        again = parse_source("".join(BIG))   # an equal string, not the same object

        self.assertIs(first, again)
        self.assertEqual(assigned_names(first)[:2], ["NAME_0", "NAME_1"])

    def test_a_file_read_twice_is_parsed_once(self):
        path = os.path.join(temp_dir(self), "module.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(BIG)
        trees = []
        for _ in range(2):
            with io.open(path, encoding="utf-8") as handle:
                trees.append(parse_source(handle.read(), filename=path))

        self.assertIs(trees[0], trees[1])

    def test_a_rewrite_with_the_same_size_and_mtime_is_parsed_afresh(self):
        """What a (path, mtime) key would get wrong: a same-tick rewrite."""
        path = os.path.join(temp_dir(self), "module.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(BIG)
        stamp = os.stat(path)
        with io.open(path, encoding="utf-8") as handle:
            before = parse_source(handle.read(), filename=path)

        rewritten = BIG.replace("NAME_0 =", "OTHER0 =")
        self.assertEqual(len(rewritten), len(BIG))
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(rewritten)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(os.stat(path).st_mtime_ns, stamp.st_mtime_ns)
        self.assertEqual(os.stat(path).st_size, stamp.st_size)
        with io.open(path, encoding="utf-8") as handle:
            after = parse_source(handle.read(), filename=path)

        self.assertIsNot(after, before)
        self.assertEqual(assigned_names(after)[0], "OTHER0")
        self.assertEqual(assigned_names(before)[0], "NAME_0", "the first caller's tree changed under it")

    def test_a_small_text_is_parsed_as_ast_parse_would(self):
        tree = parse_source("X = 1\n", filename="small.py")

        self.assertEqual(ast.dump(tree), ast.dump(ast.parse("X = 1\n")))

    def test_a_syntax_error_still_names_the_file(self):
        with self.assertRaises(SyntaxError) as raised:
            parse_source("def broken(:\n" + BIG, filename="broken.py")

        self.assertEqual(raised.exception.filename, "broken.py")


class NoTestChangesASharedTree(unittest.TestCase):

    def test_no_test_module_subclasses_a_node_transformer(self):
        offenders = []
        for name, tree in scanned_test_modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        base_name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                        if base_name == "NodeTransformer":
                            offenders.append(f"{name}:{node.lineno}")

        self.assertEqual(offenders, [], "a NodeTransformer edits the tree it walks, and the trees are shared")

    def test_no_test_module_calls_a_helper_that_edits_a_tree(self):
        offenders = []
        for name, tree in scanned_test_modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if called in TREE_EDITORS:
                        offenders.append(f"{name}:{node.lineno} {called}")

        self.assertEqual(offenders, [])


class EveryTestThatParsesWhatItReadUsesTheCache(unittest.TestCase):

    def test_no_test_module_parses_anything_but_a_literal_itself(self):
        """ast.parse("X = 1") is a snippet and costs nothing; ast.parse() of
        anything else is a text read from somewhere, and goes through
        parse_source()."""
        offenders = []
        for name, tree in scanned_test_modules():
            aliases = ast_aliases(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "parse" and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in aliases):
                    continue
                first = node.args[0] if node.args else None
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    offenders.append(f"{name}:{node.lineno}")

        self.assertEqual(offenders, [], "use tests.support.parse_source() for a text read from a file")

    def test_the_guard_sees_an_aliased_call(self):
        """Control for the guard above: one module calls `_ast.parse`."""
        tree = ast.parse("import ast as _ast\n"
                         "def f(handle):\n"
                         "    return _ast.parse(handle.read())\n")
        aliases = ast_aliases(tree)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "parse"
                 and isinstance(node.func.value, ast.Name) and node.func.value.id in aliases]

        self.assertEqual(aliases, {"_ast"})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
