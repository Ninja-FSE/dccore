"""Which copy the daemon serves when a filename is not unique.

dcc.handle_download_request() resolves a bare filename in three steps:

  1. FILE_DIRECTORY/<name> - misses for any library that keeps files in folders
  2. the master list: find the name, take the folder heading above it
  3. os.walk(FILE_DIRECTORY), first hit wins

Step 2 is the only one that knows anything. The list is where the requester
read the name from, so the folder the list puts it under is the copy they
asked for. Step 3 is a last-resort scan in whatever order the filesystem
hands back - an order the operator cannot see, control, or predict from the
listing they published.

The two only disagree when the same filename exists in more than one folder.
Step 2 had a bug that made it never match at all, so step 3 answered every
request - including the ones step 2 would have answered differently.

WHY THE FIXTURE DERIVES THE WALK ORDER INSTEAD OF ASSUMING IT

These tests need the two resolvers to disagree, which means knowing which
folder os.walk reaches first. That is the FILESYSTEM's choice and not ours:
NTFS returns directories in roughly alphabetical order, while ext4's htree
gives a hash order with no relation to the names. An earlier version of this
file hardcoded "Alpha Album comes first", which held on Windows and failed
deterministically on ext4.

So the order is measured at setUp and the list is built against it. What the
tests assert - that the list wins over the walk - is then true on any
filesystem, rather than only on the one they were written on.
"""

import io
import os
import unittest

from tests import support  # noqa: F401  (path setup)

import defaults as config  # noqa: E402
import dcc  # noqa: E402

from tests.test_path_security import InlineThread, PathSecurityBase, quiet  # noqa: E402

NAME = "Track 01.flac"
ALBUMS = ("Alpha Album", "Zebra Album")


class TwoFoldersOneFilename(PathSecurityBase):
    """The same track name under two albums, with the list and the filesystem
    deliberately disagreeing about which one to serve."""

    def setUp(self):
        super().setUp()
        for folder in ALBUMS:
            self._album(folder)

        # Measured, not assumed - see the module docstring.
        self.walk_first = self._walk_reaches_first()
        self.list_first = next(f for f in ALBUMS if f != self.walk_first)
        self._write_list([self.list_first, self.walk_first])

    # -- fixture ----------------------------------------------------------

    def _album(self, folder):
        """One album holding the shared name. The file's contents are its own
        folder name, so a test can tell which copy was actually opened."""
        directory = os.path.join(self.tree.music, folder)
        os.makedirs(directory, exist_ok=True)
        with io.open(os.path.join(directory, NAME), "w", encoding="utf-8") as handle:
            handle.write(folder)
        return os.path.join(directory, NAME)

    def _path(self, folder):
        return os.path.join(self.tree.music, folder, NAME)

    def _walk_reaches_first(self):
        """The folder os.walk hands back first on THIS filesystem."""
        for root, _dirs, files in os.walk(self.tree.music):
            if NAME in files:
                return os.path.basename(root)
        self.fail("fixture invariant: neither copy was written to disk")

    def _write_list(self, folders, sizes=None):
        """A master list in the shape update_list.py writes: a folder heading
        wrapped in rule lines, then one "!<nick> <file>  ::INFO:: <size>" row
        per file. The "D:\\MUSIC\\" prefix is the fixed heading update_list.py
        emits whatever the library's real path is, not a real directory.

        `sizes`, when given, maps folder -> its own ::INFO:: size string, for
        tests that need the duplicate copies to genuinely differ in size
        rather than sharing the fixture's default "1.0MB"."""
        rule = "=" * 53
        lines = ["List of files generated on Jan 1st\n", "\n"]
        for folder in folders:
            size = (sizes or {}).get(folder, "1.0MB")
            lines += ["\n", rule + "\n",
                      "D:\\MUSIC\\%s\\\n" % folder,
                      rule + "\n",
                      "!%s %s  ::INFO:: %s\n" % (config.NICKNAME, NAME, size)]
        path = os.path.join(self.tree.lists,
                            "%s-2026-01-01.txt" % config.LIST_BASE_NAME)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        return path

    def _request(self, name):
        self.notices.clear()
        InlineThread.dispatched = []
        config.dcc_queue.clear()
        with quiet():
            dcc.handle_download_request(self.sock, "dave", name, "#dccore-test")
        return [kind for kind, _args in self.notices]

    def _served_path(self):
        """The path the daemon settled on, whether it dispatched at once or
        queued the row - both carry the same resolved path."""
        for name, args in InlineThread.dispatched:
            if name == "start_dcc_send":
                return args[2]
        for rows in config.dcc_queue.values():
            for row in rows:
                if not row.get("is_temporary_zip"):
                    return row.get("path")
        return None

    # -- the behaviour under test -----------------------------------------

    def test_the_copy_the_list_names_first_is_the_one_served(self):
        """The requester copied the name out of the list, so the list's first
        answer is the copy they meant."""
        self._request(NAME)

        self.assertEqual(
            self._served_path(), self._path(self.list_first),
            "served the copy the filesystem happened to reach first, not the "
            "one the published list names first")

    def test_the_two_resolvers_genuinely_disagree(self):
        """Fixture invariant, and the discriminator for the test above: the
        list must name a different folder from the one the walk reaches, or
        that test would pass without proving anything."""
        self.assertNotEqual(self.list_first, self.walk_first)
        self.assertIn(self.walk_first, ALBUMS)

        found = None
        for root, _dirs, files in os.walk(self.tree.music):
            if NAME in files:
                found = os.path.basename(root)
                break

        self.assertEqual(found, self.walk_first,
                         "the walk order changed between setUp and now")

    def test_reordering_the_list_changes_which_copy_is_served(self):
        """The list is genuinely being read, rather than the right answer
        arriving for some other reason."""
        self._write_list([self.walk_first, self.list_first])

        self._request(NAME)

        self.assertEqual(self._served_path(), self._path(self.walk_first))

    def test_the_served_copy_is_the_one_whose_contents_go_out(self):
        """The path is not just cosmetic - it is what gets read off disk."""
        self._request(NAME)

        with io.open(self._served_path(), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.list_first)

    def test_no_error_is_reported_for_a_duplicate(self):
        """Documents the behaviour rather than asking for it: a duplicate is
        resolved silently, not refused. Worth pinning so that a future change
        to refuse instead is a deliberate decision and not a surprise."""
        kinds = self._request(NAME)

        self.assertNotIn("error", kinds)

    def test_a_size_hint_picks_the_matching_copy_over_the_first(self):
        """A request built from a search result's own line - the name plus
        its "  ::INFO:: <size>" tail - reaches the copy that size names, even
        when it is not the one the list would otherwise serve first."""
        self._write_list([self.list_first, self.walk_first],
                          sizes={self.list_first: "1.0MB", self.walk_first: "2.0MB"})

        self._request("%s  ::INFO:: 2.0MB" % NAME)

        self.assertEqual(self._served_path(), self._path(self.walk_first))

    def test_an_unmatched_size_hint_falls_back_to_the_first_copy(self):
        """A size that names none of the duplicates is not an error - the
        request behaves exactly as if no size had been given at all."""
        self._request("%s  ::INFO:: 9.9MB" % NAME)

        self.assertEqual(self._served_path(), self._path(self.list_first))

    def test_a_bare_request_stops_at_the_first_match(self):
        """No size hint means the first list line that matches is already the
        answer - the scan must not keep going and resolve later copies' folders
        too, since on a library with tens of thousands of lines that is the
        difference between a lookup that stops instantly and one that walks
        the whole file on every bare-name request (what AutoQ.mrc sends)."""
        third = "Mid Album"
        self._album(third)
        self._write_list([self.list_first, third, self.walk_first])

        resolved = []
        original = dcc.list_mod.resolve_list_folder

        def counting_resolve(*args, **kwargs):
            resolved.append(1)
            return original(*args, **kwargs)

        dcc.list_mod.resolve_list_folder = counting_resolve
        try:
            self._request(NAME)
        finally:
            dcc.list_mod.resolve_list_folder = original

        self.assertEqual(len(resolved), 1,
                          "a bare request resolved more than one copy's folder")

    # -- controls ----------------------------------------------------------

    def test_a_unique_filename_in_a_folder_still_resolves(self):
        """The ordinary case, which is every request on a clean library."""
        directory = os.path.join(self.tree.music, "Solo Album")
        os.makedirs(directory, exist_ok=True)
        with io.open(os.path.join(directory, "Only Here.flac"), "w",
                     encoding="utf-8") as handle:
            handle.write("SOLO")
        with io.open(os.path.join(self.tree.lists,
                                  "%s-2026-01-01.txt" % config.LIST_BASE_NAME),
                     "a", encoding="utf-8") as handle:
            rule = "=" * 53
            handle.write("\n%s\nD:\\MUSIC\\Solo Album\\\n%s\n" % (rule, rule))
            handle.write("!%s Only Here.flac  ::INFO:: 1.0MB\n" % config.NICKNAME)

        kinds = self._request("Only Here.flac")

        self.assertNotIn("error", kinds)
        self.assertEqual(self._served_path(),
                         os.path.join(directory, "Only Here.flac"))

    def test_a_missing_file_is_still_refused(self):
        """Without this, "no error" above could pass for a build that never
        refuses anything."""
        self.assertIn("error", self._request("No Such Track At All.flac"))


if __name__ == "__main__":
    unittest.main()
