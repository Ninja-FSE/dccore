"""The behavioural half of the low-severity audit findings (#232-#234).

Most of those three issues are prose - comments that no longer match the code,
messages that misstate what happened. Prose changes are checked by reading.
Four of them are not prose, and those are here.

Each was verified against the source before being fixed; roughly a third of the
batch turned out on inspection to be already fixed or not worth changing, and
those were left alone rather than churned.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402


class AMuteIsNotLabelledAsABan(unittest.TestCase):
    """A 30-second mute and the escalation ban both used category="TBAN", so
    both rendered as [TEMPBAN]. An operator watching the console could not tell
    a slap from a sentence - which is the whole reason the tag exists."""

    def tag_line(self, category):
        """The tag_str assignment inside one category's branch.

        The ASSIGNMENT, not the whole block: my first version scanned the
        branch text and flagged the comment explaining this very change, which
        is the same "cannot tell a mention from the thing" mistake #202 was
        about, made twice more in this session already.
        """
        with io.open(os.path.join(REPO_ROOT, "announce.py"), encoding="utf-8") as handle:
            lines = handle.read().split(chr(10))

        start = next(n for n, l in enumerate(lines)
                     if f'category.upper() == "{category}"' in l)
        for line in lines[start:start + 12]:
            if line.strip().startswith("tag_str"):
                return line
        raise AssertionError(f"no tag_str assignment under {category}")

    def test_the_two_categories_are_distinct_in_the_source_map(self):
        """The formatter is a chain of elif on category, so what matters is
        that MUTE and TBAN reach different branches."""
        with io.open(os.path.join(REPO_ROOT, "announce.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn('category.upper() == "MUTE"', source)
        self.assertIn('category.upper() == "TBAN"', source)

    def test_they_render_different_tags(self):
        mute = self.tag_line("MUTE")
        tban = self.tag_line("TBAN")

        self.assertIn("[MUTED]", mute)
        self.assertIn("[TEMPBAN]", tban)
        self.assertNotEqual(mute, tban)

    def test_the_mute_path_uses_the_mute_category(self):
        """The rename is worthless if the caller still says TBAN."""
        with io.open(os.path.join(REPO_ROOT, "security.py"), encoding="utf-8") as handle:
            lines = handle.read().split("\n")

        mute_line = next(n for n, l in enumerate(lines)
                         if "Triggered temporary mute" in l)
        following = "\n".join(lines[mute_line:mute_line + 3])

        self.assertIn('category="MUTE"', following)


class AnUnauthorisedUnbanIsLogged(DCCoreTestCase):
    """Every sibling admin handler prints a [SECURITY] line. This one returned
    silently, so an operator auditing attempted privilege abuse had a blind
    spot on exactly one command."""

    def run_unban(self, user="stranger"):
        import contextlib

        self.set_config(ADMIN_NICK="operator")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            commands.handle_hard_unban_request(user, "#chan", "!unban *!*@x.net")
        return buffer.getvalue()

    def test_a_stranger_is_logged(self):
        output = self.run_unban()

        self.assertIn("SECURITY", output)
        self.assertIn("stranger", output)
        self.assertIn("unban", output.lower())

    def test_the_admin_is_not_logged_as_unauthorised(self):
        """Control: the line must mark refusals, not every use."""
        output = self.run_unban(user="operator")

        self.assertNotIn("Unauthorised", output)


class TheUpdateCounterReadsTheSameFileAsEverythingElse(DCCoreTestCase):
    """count_from_master_list() excluded "-RAR-" but not the delivered "-FULL-"
    copy, unlike list.find_latest_list() which excludes both. "-FULL-" sorts
    after a plain date suffix, so the !update counter could read a different
    file from the one @find and the advert read."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="Bot", NICKNAME="Bot")

    def write(self, name, count):
        with io.open(os.path.join(self.tree.lists, name), "w",
                     encoding="utf-8") as handle:
            handle.write(f"List of {count} Files\n")

    def test_the_delivered_full_copy_is_not_counted(self):
        self.write("Bot-2026-01-01.txt", 10)
        self.write(f"Bot{list_mod.FULL_LIST_MARKER}2026-01-01.txt", 999)

        self.assertEqual(commands.count_from_master_list(), 10)

    def test_it_agrees_with_find_latest_list(self):
        """The property that matters: two functions, one answer. They differed
        only by accident before, because the delivered copy happens to carry
        the same header."""
        self.write("Bot-2026-01-01.txt", 42)
        self.write(f"Bot{list_mod.FULL_LIST_MARKER}2026-01-01.txt", 999)

        index = list_mod.find_latest_list()

        self.assertEqual(os.path.basename(index), "Bot-2026-01-01.txt")
        self.assertEqual(commands.count_from_master_list(), 42)

    def test_the_rar_list_is_still_excluded(self):
        """Control for the filter that was already right."""
        self.write("Bot-2026-01-01.txt", 10)
        self.write("Bot-RAR-2026-09-01.txt", 900)

        self.assertEqual(commands.count_from_master_list(), 10)


class AFailedCleanupIsNotSilent(unittest.TestCase):
    """A partial file that cannot be removed after a failed fetch left no trace
    at all, so disk use climbed with no clue why. webserver.py's equivalent
    cleanup already printed on the same failure."""

    def test_the_partial_file_cleanup_reports_its_failure(self):
        """Located structurally: the try whose body removes dest_path, then its
        handlers. Matching that block by text is what my first attempt did, and
        it was brittle enough that a mutation of the very block it guards did
        not move it."""
        import ast

        with io.open(os.path.join(REPO_ROOT, "dcc_fetch.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        guarded = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            removes_dest = any(
                isinstance(inner, ast.Call)
                and getattr(inner.func, "attr", None) == "remove"
                and "dest_path" in ast.unparse(inner)
                for inner in ast.walk(node)
            )
            if removes_dest:
                guarded.append(node)

        self.assertTrue(guarded,
                        "no try/except around the dest_path removal was found - "
                        "the scan is looking at the wrong shape")

        for node in guarded:
            for handler in node.handlers:
                # Does it REPORT, not "is the body exactly one Pass". The first
                # version asked the latter and missed the mutant, whose handler
                # happens to carry a second statement after the pass - so the
                # shape check said "not bare" about a handler that says nothing.
                reports = any(
                    isinstance(inner, ast.Call)
                    and (getattr(inner.func, "id", None) == "print"
                         or getattr(inner.func, "attr", None) in ("send_debug", "warning",
                                                                  "error", "exception"))
                    for statement in handler.body
                    for inner in ast.walk(statement))

                self.assertTrue(
                    reports,
                    f"dcc_fetch.py:{handler.lineno} swallows a failed removal of "
                    f"the partial file without saying so - disk use climbs after "
                    f"failed fetches and nothing explains why")


if __name__ == "__main__":
    unittest.main()
