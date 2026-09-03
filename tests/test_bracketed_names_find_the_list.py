"""A nickname or a directory containing [ ] still finds the master list.

`Bot[GR]`, `Nick[away]`, `DCCore[NL]` - brackets in a nick are an ordinary IRC
convention, and `LIST_BASE_NAME` follows `NICKNAME` by default (defaults.py's
`if LIST_BASE_NAME == "DCCore" and NICKNAME:`). A share mounted at
`D:\\Lists[FLAC]\\` is the same character arriving from the other direction.

find_latest_list() interpolated both values straight into a glob pattern, where
"[" opens a character class. `Bot[GR]-*.txt` asks glob for a file called `BotG`
or `BotR` followed by `-`, so it matched nothing - and matching nothing is not
an error. No exception, no warning, no log line. @find answered "No MasterList
found" and the five-minute advert published "For My List Of: 0 Files" into
every channel, indefinitely.

WHAT THIS DOES NOT AFFECT, WHICH IS WHY IT COULD SURVIVE

The artifact @<nick> actually sends is found by find_latest_list_file(), which
walks os.listdir() and compares with str.startswith. That path never used glob,
so the bot went on handing out its list file normally while insisting it had
zero files in it - the two disagree, and only the visible half looked healthy.

The pair below is the whole point: the delivered file keeps working (it always
did), and the index no longer disagrees with it.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import commands  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


# Brackets are legal in a filename everywhere, so these run on both platforms.
BRACKETED_NAMES = [
    "Bot[GR]",          # the case that motivated this
    "Nick[away]",
    "DCCore[NL]",
    "Bot[a-z]",         # a range, so an unescaped glob matches a REAL neighbour
]

# "*" and "?" are glob wildcards too, and LIST_BASE_NAME is free text an
# operator can set to anything. They are also RESERVED characters on Windows -
# the file cannot be created, so the defect cannot occur there at all. Probed
# rather than assumed: asserting a filesystem's behaviour universally has been
# wrong here three times (loopback, console code page, MAX_PATH).
WILDCARD_NAMES = ["Bot?One", "Bot*Two"]


def filesystem_accepts(directory, name):
    """True if this filesystem will create a file with `name` in it."""
    path = os.path.join(directory, name)
    try:
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("probe")
    except OSError:
        return False
    os.remove(path)
    return True


class TheIndexIsFoundWhateverTheNameContains(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)

    def write_index(self, base_name, date="2026-01-01"):
        """Write a master text list exactly as update_list.py names it."""
        path = os.path.join(self.tree.lists, f"{base_name}-{date}.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("List of 3 Files\n!bot Track One.flac\n")
        return path

    def test_a_bracketed_name_still_finds_its_index(self):
        for name in BRACKETED_NAMES:
            with self.subTest(name=name):
                self.set_config(LIST_BASE_NAME=name, NICKNAME=name,
                                LOCAL_LIST_DIR=self.tree.lists)
                written = self.write_index(name)
                try:
                    self.assertEqual(list_mod.find_latest_list(), written)
                finally:
                    os.remove(written)

    def test_a_wildcard_in_the_name_still_finds_its_index(self):
        """Only where the filesystem allows such a file to exist. On Windows
        it cannot, so there is nothing to get wrong and the case is skipped
        rather than asserted."""
        ran = 0
        for name in WILDCARD_NAMES:
            if not filesystem_accepts(self.tree.lists, name):
                continue
            with self.subTest(name=name):
                ran += 1
                self.set_config(LIST_BASE_NAME=name, NICKNAME=name,
                                LOCAL_LIST_DIR=self.tree.lists)
                written = self.write_index(name)
                try:
                    self.assertEqual(list_mod.find_latest_list(), written)
                finally:
                    os.remove(written)
        if not ran:
            self.skipTest("this filesystem reserves * and ? in filenames")

    def test_a_bracketed_directory_still_finds_its_index(self):
        """The same character from the other side. An operator on Windows
        naming a folder Lists[FLAC] is doing nothing unusual."""
        awkward_dir = os.path.join(self.tree.root, "Lists[FLAC]")
        os.makedirs(awkward_dir, exist_ok=True)
        self.set_config(LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
                        LOCAL_LIST_DIR=awkward_dir)
        path = os.path.join(awkward_dir, "DCCoreTest-2026-01-01.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("List of 3 Files\n")

        self.assertEqual(list_mod.find_latest_list(), path)

    def test_the_file_count_is_not_zero(self):
        """What the channel actually saw. find_latest_list() returning None
        surfaces as the advert announcing a library of nothing."""
        self.set_config(LIST_BASE_NAME="Bot[GR]", NICKNAME="Bot[GR]",
                        LOCAL_LIST_DIR=self.tree.lists)
        self.write_index("Bot[GR]")

        count, _date, _size, _raw = list_mod.get_file_count_date_size_and_raw_bytes()

        self.assertEqual(count, 1, "the advert would publish 0 Files")

    def test_a_bracketed_name_does_not_match_a_neighbours_list(self):
        """The mirror image, and the reason escaping beats a bare replace.
        Unescaped, "Bot[a-z]-*.txt" is a RANGE: it matches Bota-, Botb- ... so
        a second bot sharing the directory could have its index served as this
        one's."""
        self.set_config(LIST_BASE_NAME="Bot[a-z]", NICKNAME="Bot[a-z]",
                        LOCAL_LIST_DIR=self.tree.lists)
        neighbour = os.path.join(self.tree.lists, "Botx-2026-06-01.txt")
        with io.open(neighbour, "w", encoding="utf-8") as handle:
            handle.write("List of 999 Files\n")

        self.assertIsNone(list_mod.find_latest_list(),
                          "matched another bot's list through a glob range")

    def test_an_ordinary_name_is_unaffected(self):
        """Control. Escaping must not change the normal case."""
        self.set_config(LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
                        LOCAL_LIST_DIR=self.tree.lists)
        written = self.write_index("DCCoreTest")

        self.assertEqual(list_mod.find_latest_list(), written)

    def test_the_newest_still_wins(self):
        """Control for the sort, which the escaping sits in front of."""
        self.set_config(LIST_BASE_NAME="Bot[GR]", NICKNAME="Bot[GR]",
                        LOCAL_LIST_DIR=self.tree.lists)
        self.write_index("Bot[GR]", date="2026-01-01")
        newest = self.write_index("Bot[GR]", date="2026-07-01")

        self.assertEqual(list_mod.find_latest_list(), newest)

    def test_the_rar_list_is_still_excluded(self):
        """Control for the filter downstream of the glob: the album list must
        not be mistaken for the index, or the advert counts albums as files."""
        self.set_config(LIST_BASE_NAME="Bot[GR]", NICKNAME="Bot[GR]",
                        LOCAL_LIST_DIR=self.tree.lists)
        index = self.write_index("Bot[GR]", date="2026-01-01")
        rar_list = os.path.join(self.tree.lists, "Bot[GR]-RAR-2026-09-01.txt")
        with io.open(rar_list, "w", encoding="utf-8") as handle:
            handle.write("List of 40 Files\n")

        self.assertEqual(list_mod.find_latest_list(), index)


class TheDeliveredArtifactWasNeverAffected(DCCoreTestCase):
    """The half that kept working, pinned so the disagreement cannot come back
    from the other side either."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.set_config(LIST_BASE_NAME="Bot[GR]", NICKNAME="Bot[GR]",
                        LOCAL_LIST_DIR=self.tree.lists, LIST_FORMAT="zip")

    def test_the_zip_is_still_served(self):
        path = os.path.join(self.tree.lists, "Bot[GR]-2026-01-01.zip")
        with io.open(path, "wb") as handle:
            handle.write(b"PK\x05\x06" + b"\x00" * 18)

        self.assertEqual(list_mod.find_latest_list_file(), path)


class TheUpdateCounterReadsTheSameIndex(DCCoreTestCase):
    """!update reports "X files, added Y" by reading line 1 before and after the
    rebuild. It globs the same two values, so it had the same defect - and it
    reported "0 files, added 0" while the advert published the real total.

    This is the half a mutation run caught and the list.py tests could not:
    the counter was a closure inside handle_list_update_request(), so reaching
    it meant running a real subprocess. It is a module-level function now.
    """

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)

    def write_index(self, base_name, count):
        path = os.path.join(self.tree.lists, f"{base_name}-2026-01-01.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(f"List of {count} Files\n")
        return path

    def test_a_bracketed_name_counts_its_files(self):
        for name in BRACKETED_NAMES:
            with self.subTest(name=name):
                self.set_config(LIST_BASE_NAME=name, NICKNAME=name,
                                LOCAL_LIST_DIR=self.tree.lists)
                written = self.write_index(name, "1,234")
                try:
                    self.assertEqual(commands.count_from_master_list(), 1234)
                finally:
                    os.remove(written)

    def test_a_bracketed_directory_counts_its_files(self):
        awkward_dir = os.path.join(self.tree.root, "Lists[FLAC]")
        os.makedirs(awkward_dir, exist_ok=True)
        self.set_config(LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
                        LOCAL_LIST_DIR=awkward_dir)
        with io.open(os.path.join(awkward_dir, "DCCoreTest-2026-01-01.txt"),
                     "w", encoding="utf-8") as handle:
            handle.write("List of 77 Files\n")

        self.assertEqual(commands.count_from_master_list(), 77)

    def test_an_ordinary_name_is_unaffected(self):
        """Control."""
        self.set_config(LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
                        LOCAL_LIST_DIR=self.tree.lists)
        self.write_index("DCCoreTest", "42")

        self.assertEqual(commands.count_from_master_list(), 42)

    def test_no_list_counts_zero_rather_than_raising(self):
        """Control for the except: !update must not die because the list is
        missing - it is about to rebuild it."""
        self.set_config(LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
                        LOCAL_LIST_DIR=self.tree.lists)

        self.assertEqual(commands.count_from_master_list(), 0)

    def test_the_rar_list_is_not_counted(self):
        """Control for the filter downstream of the glob."""
        self.set_config(LIST_BASE_NAME="Bot[GR]", NICKNAME="Bot[GR]",
                        LOCAL_LIST_DIR=self.tree.lists)
        self.write_index("Bot[GR]", "10")
        with io.open(os.path.join(self.tree.lists, "Bot[GR]-RAR-2026-09-01.txt"),
                     "w", encoding="utf-8") as handle:
            handle.write("List of 900 Files\n")

        self.assertEqual(commands.count_from_master_list(), 10)


if __name__ == "__main__":
    unittest.main()
