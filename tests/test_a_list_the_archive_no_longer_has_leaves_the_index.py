"""A list the archive no longer has leaves the search index, and Forget takes
every list a bot ever had.

A fetch indexes each further list of an archive under "<nick>/<marker>" and
records only the markers it kept. A refetch whose archive no longer had the
"rar" list - or had it empty, or over the size ceiling - replaced the entry
and left "tunebox/rar" in the index, rows and folders. forget_bot() dropped
only the markers the current entry named, so a later Forget left them too,
and nothing else ever removed them: a disk leak the size of that list's
index, and rows whose `bot:"tunebox"` phrase also matched the bare nick in
every search.

Now a successful install drops every list the previous entry had and this one
did not keep, and Forget drops every name the index holds under the nick -
the bare nick and "<nick>/" followed by anything - and nothing that only
starts with the same letters.
"""

import io
import os
import sqlite3
import zipfile

from tests import support  # noqa: F401  (path setup)

import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import list_index  # noqa: E402

BS = chr(92)
NL = chr(10)


def list_text(base, names, folder):
    out = ["Header" + NL, "=" * 30 + NL, folder + NL, "=" * 30 + NL]
    for name in names:
        out.append(f"!{base} {name}  ::INFO:: 5000000" + NL)
    return "".join(out)


def folders_of(name):
    """The folder rows kept for one index name (#1135), read straight from
    the file so nothing in list_index can hide them."""
    list_index.close()
    conn = sqlite3.connect(config.LIST_INDEX_FILE)
    try:
        return [row[0] for row in conn.execute(
            "SELECT folder FROM folders WHERE bot = ?", (name,))]
    finally:
        conn.close()


class IndexAndFetchCase(support.DCCoreTestCase):

    def setUp(self):
        super().setUp()
        root = self.make_temp_dir(prefix="dccore-dropped-lists-")
        self.set_config(FETCHED_FILES_DIR=os.path.join(root, "fetched"),
                        LIST_INDEX_FILE=os.path.join(root, "idx.db"))
        list_index.reset_for_tests()
        self.addCleanup(list_index.reset_for_tests)

    def fetch(self, nick, lists):
        """One archive, {marker: [file names]}; "Default" is the main list."""
        os.makedirs(config.FETCHED_FILES_DIR, exist_ok=True)
        path = os.path.join(config.FETCHED_FILES_DIR, "incoming.zip")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            for marker, names in lists.items():
                folder = "D:" + BS + "Music" + BS + marker + " Folder"
                archive.writestr(f"{nick}-{marker}(2026-01-02)-OS.txt",
                                 list_text(nick, names, folder))
        with open(path, "wb") as handle:
            handle.write(buf.getvalue())
        ok, reason = list_fetch.process_fetched_list_zip(nick, path)
        self.assertTrue(ok, reason)

    def index(self, name, *filenames):
        rows = [{"title": f, "folder": "Some Folder", "size": "4MB"}
                for f in filenames]
        list_index.index_bot_list(name, rows)


class ARefetch(IndexAndFetchCase):

    def test_drops_a_list_the_archive_no_longer_has(self):
        self.fetch("TuneBox", {"Default": ["Love One.flac"],
                               "rar": ["Love Two.flac"]})
        self.assertEqual(list_index.indexed_bots(), {"tunebox", "tunebox/rar"})

        self.fetch("TuneBox", {"Default": ["Love One.flac"]})

        self.assertEqual(list_index.indexed_bots(), {"tunebox"})
        self.assertEqual(folders_of("tunebox/rar"), [])

    def test_drops_one_that_is_now_empty(self):
        """_measure_extra_list() skips an empty list without indexing it, so
        its old rows were never replaced either."""
        self.fetch("TuneBox", {"Default": ["Love One.flac"],
                               "rar": ["Love Two.flac"]})

        self.fetch("TuneBox", {"Default": ["Love One.flac"], "rar": []})

        self.assertEqual(list_index.indexed_bots(), {"tunebox"})

    def test_keeps_a_list_it_kept(self):
        self.fetch("TuneBox", {"Default": ["Love One.flac"],
                               "rar": ["Love Two.flac"],
                               "films": ["Love Film.mkv"]})

        self.fetch("TuneBox", {"Default": ["Love One.flac"],
                               "rar": ["Love Three.flac"]})

        self.assertEqual(list_index.indexed_bots(), {"tunebox", "tunebox/rar"})
        self.assertEqual(
            [row["filename"] for row in list_index.search(
                ["love three"], bots=["TuneBox/rar"])],
            ["Love Three.flac"])

    def test_keeps_another_bots_lists(self):
        self.fetch("TuneBox2", {"Default": ["Love One.flac"],
                                "rar": ["Love Two.flac"]})
        self.fetch("TuneBox", {"Default": ["Love One.flac"],
                               "rar": ["Love Two.flac"]})

        self.fetch("TuneBox", {"Default": ["Love One.flac"]})

        self.assertEqual(list_index.indexed_bots(),
                         {"tunebox", "tunebox2", "tunebox2/rar"})

    def test_the_main_list_is_found_again_afterwards(self):
        """What the stale rows cost besides disk: `bot:"tunebox"` matched
        them, and 300 of them filled the page before the main list's rows.
        The main list is the archive's largest, so it gets more files that do
        not match."""
        main = ["Love Main.flac"] + [f"Other {n:03d}.flac" for n in range(400)]
        self.fetch("TuneBox", {"Default": main,
                               "rar": [f"Love {n:03d}.flac" for n in range(300)]})

        self.fetch("TuneBox", {"Default": main})

        self.assertEqual(
            [row["filename"] for row in list_index.search(
                ["love"], limit=200, bots=["TuneBox"])],
            ["Love Main.flac"])


class Forget(IndexAndFetchCase):

    def test_drops_a_list_the_entry_no_longer_names(self):
        """The rows a refetch from before this fix left behind: in the index,
        and in no entry."""
        self.fetch("TuneBox", {"Default": ["Love One.flac"]})
        self.index("TuneBox/rar", "Love Two.flac")
        self.index("TuneBox/films", "Love Film.mkv")

        self.assertTrue(list_fetch.forget_bot("TuneBox"))

        self.assertEqual(list_index.indexed_bots(), set())
        self.assertEqual(folders_of("tunebox/rar"), [])
        self.assertEqual(folders_of("tunebox"), [])

    def test_keeps_a_nick_that_only_starts_the_same(self):
        self.fetch("TuneBox", {"Default": ["Love One.flac"]})
        self.index("TuneBox2", "Love Two.flac")
        self.index("TuneBox2/rar", "Love Two.flac")
        self.index("TuneBoxrar", "Love Two.flac")

        list_fetch.forget_bot("TuneBox")

        self.assertEqual(list_index.indexed_bots(),
                         {"tunebox2", "tunebox2/rar", "tuneboxrar"})
        self.assertEqual(folders_of("tunebox2/rar"), ["Some Folder"])

    def test_an_underscore_in_the_nick_is_not_a_wildcard(self):
        """"_" matches any one character in LIKE, and is common in a nick."""
        self.fetch("Tune_Box", {"Default": ["Love One.flac"]})
        self.index("TuneXBox/rar", "Love Two.flac")
        self.index("Tune_Box/rar", "Love Two.flac")

        list_fetch.forget_bot("Tune_Box")

        self.assertEqual(list_index.indexed_bots(), {"tunexbox/rar"})

    def test_a_percent_sign_in_the_nick_is_not_a_wildcard(self):
        self.index("Tune%", "Love One.flac")
        self.index("Tunebox/rar", "Love Two.flac")

        self.assertTrue(list_index.drop_every_list_of("Tune%"))

        self.assertEqual(list_index.indexed_bots(), {"tunebox/rar"})

    def test_a_nick_with_no_letters_or_digits(self):
        self.index("^_^", "Love One.flac")
        self.index("^_^/rar", "Love Two.flac")
        self.index("^_^^", "Love Three.flac")

        self.assertTrue(list_index.drop_every_list_of("^_^"))

        self.assertEqual(list_index.indexed_bots(), {"^_^^"})

    def test_the_nick_is_compared_without_case(self):
        self.index("TuneBox", "Love One.flac")
        self.index("TuneBox/rar", "Love Two.flac")

        self.assertTrue(list_index.drop_every_list_of(" TUNEBOX "))

        self.assertEqual(list_index.indexed_bots(), set())

    def test_no_nick_drops_nothing(self):
        self.index("TuneBox", "Love One.flac")

        self.assertFalse(list_index.drop_every_list_of("  "))

        self.assertEqual(list_index.indexed_bots(), {"tunebox"})
