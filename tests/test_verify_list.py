"""Verify list - filenames the master list carries under more than one folder.

Kept in one file rather than split across test_list/test_adminchat/
test_webserver, because the three parts only mean anything together: a finder,
and two surfaces that must agree about what it found. A duplicate reported in
the dashboard but not in the console - or the other way round - is the bug
this arrangement is meant to make obvious.

WHY THE FEATURE EXISTS

A request that names a file alone is all a requester can send from the list on
its own: "!<nick> Track 01.flac". dcc.handle_download_request() resolves that
name against the same list and serves the FIRST folder it finds it under, so
every later copy is listed, looks requestable, and is shadowed by the first.

Since #128 that is the whole of the claim rather than more than it. A requester
who pastes a search result's entire line - its "  ::INFO:: <size>" tail
included - reaches the copy that size names. The tool used to say those copies
were "unreachable", which stopped being true; it says shadowed now, and the
payload field is named for what it counts.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def entry(folder, filename):
    return {"line": f"!bot {filename}", "folder": folder,
            "filename": filename, "size": "1.0MB"}


class Recorder:
    """Stands in for a DCC CHAT session. _cmd_verify only ever calls send()."""

    def __init__(self):
        self.lines = []

    def send(self, text=""):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


class TheFinder(unittest.TestCase):
    """list.find_duplicate_filenames() - the shared part."""

    def test_a_clean_list_reports_nothing(self):
        found = list_mod.find_duplicate_filenames(
            [entry("Alpha", "One.flac"), entry("Beta", "Two.flac")])

        self.assertEqual(found, [])

    def test_a_name_under_two_folders_is_reported_with_both(self):
        found = list_mod.find_duplicate_filenames(
            [entry("Alpha", "Track 01.flac"), entry("Beta", "Track 01.flac")])

        self.assertEqual(found, [{"filename": "Track 01.flac",
                                  "folders": ["Alpha", "Beta"], "count": 2}])

    def test_folders_keep_the_order_the_list_gave_them(self):
        """The first folder is the copy a request actually reaches, so the
        order carries the answer - sorting it would throw that away."""
        found = list_mod.find_duplicate_filenames(
            [entry("Zebra", "x.flac"), entry("Alpha", "x.flac")])

        self.assertEqual(found[0]["folders"], ["Zebra", "Alpha"])

    def test_three_folders_are_all_reported(self):
        found = list_mod.find_duplicate_filenames(
            [entry("A", "x.flac"), entry("B", "x.flac"), entry("C", "x.flac")])

        self.assertEqual(found[0]["count"], 3)
        self.assertEqual(found[0]["folders"], ["A", "B", "C"])

    def test_the_match_ignores_case(self):
        """dcc.py compares lowercased, and a requester retyping a name cannot
        be expected to reproduce its case - so this has to see the collision
        they would hit."""
        found = list_mod.find_duplicate_filenames(
            [entry("Alpha", "Track 01.flac"), entry("Beta", "TRACK 01.FLAC")])

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["folders"], ["Alpha", "Beta"])

    def test_the_same_name_twice_under_one_folder_is_not_a_duplicate(self):
        """A name that resolves to a single folder is unambiguous however many
        list rows carry it."""
        found = list_mod.find_duplicate_filenames(
            [entry("Alpha", "x.flac"), entry("Alpha", "x.flac")])

        self.assertEqual(found, [])

    def test_a_folderless_entry_counts_as_the_library_root(self):
        """None is a real location - the root - not a missing value. A foreign
        bot's list can arrive with no folder headings at all."""
        found = list_mod.find_duplicate_filenames(
            [entry(None, "x.flac"), entry("Alpha", "x.flac")])

        self.assertEqual(found[0]["folders"], ["", "Alpha"])

    def test_an_entry_with_no_filename_is_skipped_not_grouped(self):
        """A malformed line must not collect every other malformed line into a
        phantom duplicate under the empty name."""
        found = list_mod.find_duplicate_filenames(
            [entry("Alpha", ""), entry("Beta", ""), entry("C", "real.flac")])

        self.assertEqual(found, [])

    def test_an_empty_list_is_not_an_error(self):
        self.assertEqual(list_mod.find_duplicate_filenames([]), [])


class ListOnDisk(DCCoreTestCase):
    """Both surfaces read the configured master list, so they need a real
    one."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.LOCAL_LIST_DIR = self.tree.lists
        config.LIST_BASE_NAME = "DCCore"
        config.NICKNAME = "DCCore"

    def write_list(self, folders):
        """A master list in update_list.py's shape: each folder heading wrapped
        in rule lines, then its files. "D:\\MUSIC\\" is the fixed heading the
        generator writes whatever the library's real path is."""
        rule = "=" * 53
        lines = ["List of Files generated on Jan 1st\n", "\n"]
        for folder, files in folders:
            lines += ["\n", rule + "\n", "D:\\MUSIC\\%s\\\n" % folder, rule + "\n"]
            for filename in files:
                lines.append("!%s %s  ::INFO:: 1.0MB\n" % (config.NICKNAME, filename))
        path = os.path.join(self.tree.lists, "%s-2026-01-01.txt" % config.LIST_BASE_NAME)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        return path


class TheFolderResolver(DCCoreTestCase):
    """list.resolve_list_folder() - a list heading in, a real path out.

    The heading is not a path. update_list.py writes "D:\\MUSIC\\<folder>\\"
    into every list whatever the library's real location is, so showing it
    verbatim names a drive most installs do not have - in a tool whose entire
    job is "go and look at these folders".
    """

    def setUp(self):
        super().setUp()
        config.FILE_DIRECTORY = os.path.join("Z:" + os.sep, "1 Metal")

    def test_the_fixed_prefix_is_replaced_by_the_real_library(self):
        self.assertEqual(
            list_mod.resolve_list_folder("D:\\MUSIC\\Alpha Album\\"),
            os.path.join(config.FILE_DIRECTORY, "Alpha Album"))

    def test_nested_folders_survive_the_round_trip(self):
        self.assertEqual(
            list_mod.resolve_list_folder("D:\\MUSIC\\3 Greek\\Yovel\\2020 - X\\"),
            os.path.join(config.FILE_DIRECTORY, "3 Greek", "Yovel", "2020 - X"))

    def test_a_heading_with_no_folder_is_the_library_root(self):
        self.assertEqual(list_mod.resolve_list_folder("D:\\MUSIC\\"),
                         config.FILE_DIRECTORY)

    def test_an_entry_that_carried_no_heading_is_the_library_root(self):
        """A foreign bot's list can arrive with no folder headings at all, and
        such a file really does sit at the root."""
        self.assertEqual(list_mod.resolve_list_folder(""), config.FILE_DIRECTORY)
        self.assertEqual(list_mod.resolve_list_folder(None), config.FILE_DIRECTORY)

    def test_a_heading_without_the_prefix_is_still_treated_as_relative(self):
        """Defensive: a heading from a list that does not use the OmenServe
        prefix must not have its folder silently dropped."""
        self.assertEqual(list_mod.resolve_list_folder("Some Album\\"),
                         os.path.join(config.FILE_DIRECTORY, "Some Album"))

    def test_the_separator_is_the_hosts_own(self):
        """Headings carry backslashes on every platform, so a POSIX host must
        not end up with one embedded inside a path component."""
        resolved = list_mod.resolve_list_folder("D:\\MUSIC\\A\\B\\", base="/mnt/music")

        self.assertEqual(resolved, os.path.join("/mnt/music", "A", "B"))

    def test_the_base_can_be_given_explicitly(self):
        """Keeps the function testable without reaching into config, and lets
        a caller resolve against a library other than this bot's own."""
        self.assertEqual(
            list_mod.resolve_list_folder("D:\\MUSIC\\X\\", base="/tmp/lib"),
            os.path.join("/tmp/lib", "X"))

    def test_the_prefix_match_ignores_case(self):
        """update_list.py writes it uppercase, but a hand-edited or foreign
        list may not."""
        self.assertEqual(
            list_mod.resolve_list_folder("d:\\music\\Alpha\\"),
            os.path.join(config.FILE_DIRECTORY, "Alpha"))


    # ---- input that did not come from our own list -----------------------
    #
    # #121 gave this function a second caller with a very different shape:
    # dcc.handle_download_request()'s `!rar` path, which passes a folder the
    # USER typed into the channel. Every test above feeds it a heading our own
    # update_list.py wrote. These feed it what an attacker would.
    #
    # The contract being pinned is that resolve_list_folder() is a JOINER, not
    # a sanitiser. It exists to turn a list heading into a path, and the
    # traversal guard is is_safe_path() immediately after it at the call site.
    # Quietly normalising an escape away here would be worse than useless: the
    # request would stop being refused and start resolving to some other real
    # folder instead, which is an attacker's best outcome, not ours.

    def test_an_absolute_path_cannot_escape_the_library(self):
        """The one property worth having here rather than downstream. An
        absolute path is joined as though relative, so it lands inside the
        library instead of at the root of the disk - os.path.join()'s own
        behaviour would otherwise discard the base entirely and return
        '/etc/passwd'."""
        resolved = list_mod.resolve_list_folder("/etc/passwd", base="/srv/music")

        self.assertTrue(
            resolved.startswith("/srv/music"),
            f"an absolute path escaped the library root: {resolved!r}")

    def test_a_windows_absolute_path_cannot_escape_either(self):
        resolved = list_mod.resolve_list_folder("C:\\Windows\\System32",
                                                base="/srv/music")

        self.assertTrue(resolved.startswith("/srv/music"), resolved)

    def test_dot_dot_survives_for_the_guard_to_refuse(self):
        """Deliberately NOT normalised away.

        `is_safe_path()` at the call site resolves the result and refuses
        anything landing outside FILE_DIRECTORY, and a refusal is the correct
        answer to a traversal attempt. Collapsing the `..` here would turn that
        refusal into a successful resolution of a different folder - the same
        request quietly succeeding against something the operator never
        offered.
        """
        resolved = list_mod.resolve_list_folder("D:\\MUSIC\\..\\..\\etc\\",
                                                base="/srv/music")

        self.assertIn("..", resolved,
                      "the traversal was normalised away instead of being left "
                      "for the guard to refuse")

    def test_doubled_separators_collapse(self):
        """A heading with a doubled slash is a formatting artefact, not an
        attack, and must not produce an empty path component."""
        self.assertEqual(
            list_mod.resolve_list_folder("D:\\MUSIC\\\\Album\\", base="/srv/music"),
            os.path.join("/srv/music", "Album"))

    def test_the_rar_path_still_refuses_a_traversal_end_to_end(self):
        """The guard this function relies on, asserted rather than assumed.

        dcc.py joins the resolved folder and then calls is_safe_path() against
        the library root. That is what makes leaving `..` alone above safe, so
        it is worth one test proving the pair works together rather than two
        proving each half separately.
        """
        import dcc

        escaped = list_mod.resolve_list_folder("D:\\MUSIC\\..\\..\\etc\\",
                                               base=config.FILE_DIRECTORY)

        self.assertFalse(
            dcc.is_safe_path(config.FILE_DIRECTORY, escaped),
            "is_safe_path() accepted a path resolve_list_folder() left a "
            "traversal in - the two together are the guard")



class TheAdminConsoleCommand(ListOnDisk):
    """adminchat's `verify`. The console is one of the two surfaces an
    operator has - it is not the debug channel, and it does not need the
    dashboard enabled."""

    def run_verify(self):
        session = Recorder()
        adminchat._cmd_verify(session, "")
        return session

    def test_a_clean_list_says_so_rather_than_saying_nothing(self):
        """Silence would be indistinguishable from the command failing. A tool
        the operator ran on purpose has to answer."""
        self.write_list([("Alpha", ["One.flac"]), ("Beta", ["Two.flac"])])

        text = self.run_verify().text

        self.assertIn("unique", text)
        self.assertIn("2", text, "the number checked should be reported")

    def test_a_duplicate_is_named_with_every_folder_holding_it(self):
        self.write_list([("Alpha Album", ["Track 01.flac"]),
                         ("Zebra Album", ["Track 01.flac"])])

        text = self.run_verify().text

        self.assertIn("Track 01.flac", text)
        self.assertIn("Alpha Album", text)
        self.assertIn("Zebra Album", text)

    def test_it_explains_why_a_duplicate_matters(self):
        """An operator who does not know a request names a file rather than a
        path has no way to tell why this is worth acting on."""
        self.write_list([("A", ["x.flac"]), ("B", ["x.flac"])])

        self.assertIn("names a file alone", self.run_verify().text)

    def test_the_detail_is_capped_but_the_count_is_not(self):
        """Every line here is a DCC CHAT line. A library with hundreds of
        collisions must not flood the console session - but the operator still
        has to be told how many there were."""
        names = ["f%03d.flac" % i for i in range(adminchat.VERIFY_CONSOLE_LIMIT + 5)]
        self.write_list([("A", names), ("B", names)])

        text = self.run_verify().text

        self.assertIn(str(len(names)), text, "the full count must be reported")
        self.assertIn("f000.flac", text)
        self.assertNotIn(names[-1], text, "detail past the cap must be elided")
        self.assertIn("more", text)

    def test_a_missing_list_is_reported_rather_than_read_as_clean(self):
        """No list is not the same answer as no duplicates, and telling the
        operator their library is fine when nothing was checked is worse than
        saying nothing."""
        text = self.run_verify().text

        self.assertIn("empty or could not be read", text)
        self.assertNotIn("unique", text)

    def test_the_command_is_registered_and_documented(self):
        """An unregistered command is unreachable, and one missing from the
        table is missing from `help` - which is how an operator finds it."""
        self.assertIn("verify", adminchat.COMMANDS)
        function, summary, usage = adminchat.COMMANDS["verify"]
        self.assertIs(function, adminchat._cmd_verify)
        self.assertTrue(summary.strip())
        self.assertTrue(usage.strip())


class TheDashboardPayload(ListOnDisk):
    """webserver's /api/tools/verify-list, the other surface."""

    def test_a_clean_list_reports_what_it_checked(self):
        self.write_list([("Alpha", ["One.flac"]), ("Beta", ["Two.flac"])])

        payload = webserver.build_verify_list_payload()

        self.assertEqual(payload["duplicates"], [])
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["checked"], 2)
        self.assertEqual(payload["shadowed"], 0)

    def test_a_duplicate_comes_back_with_its_folders(self):
        self.write_list([("Alpha Album", ["Track 01.flac"]),
                         ("Zebra Album", ["Track 01.flac"])])

        payload = webserver.build_verify_list_payload()

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["duplicates"][0]["filename"], "Track 01.flac")
        self.assertEqual(
            payload["duplicates"][0]["folders"],
            [os.path.join(config.FILE_DIRECTORY, "Alpha Album"),
             os.path.join(config.FILE_DIRECTORY, "Zebra Album")],
            "the operator is shown paths on THIS machine, not the list's "
            "fixed heading")

    def test_shadowed_counts_copies_not_names(self):
        """`total` answers "how many names collide"; `shadowed` answers "how
        many copies a bare-name request can never reach", which is the one
        saying how much of the library is affected. A name in three folders
        shadows two copies."""
        self.write_list([("A", ["x.flac"]), ("B", ["x.flac"]), ("C", ["x.flac"])])

        payload = webserver.build_verify_list_payload()

        self.assertEqual(payload["total"], 1, "one name collides")
        self.assertEqual(payload["shadowed"], 2, "two of the three copies")

    def test_the_two_surfaces_agree(self):
        """The point of sharing one finder. If these ever disagree, one of the
        surfaces is lying to the operator about their library."""
        self.write_list([("A", ["x.flac", "y.flac"]), ("B", ["x.flac"])])

        payload = webserver.build_verify_list_payload()
        console = Recorder()
        adminchat._cmd_verify(console, "")

        self.assertEqual(payload["total"], 1)
        for item in payload["duplicates"]:
            self.assertIn(item["filename"], console.text)
            for folder in item["folders"]:
                self.assertIn(folder, console.text)


if __name__ == "__main__":
    unittest.main()
