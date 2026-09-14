"""Two things the scan did to every file in the library.

  * #463 generate_master_list() built a SECOND full copy of the library - one
    dict per file - and handed it to list.find_duplicate_filenames(), whose
    answer was passed to len() and dropped. Measured at 5.4M files: 2.2 GiB
    for the copy, and a 3.5 GiB peak for the rebuild. The same pass also
    recomputed each directory's relative path once per FILE rather than once
    per directory, and stored a separate equal string in every row.
  * #464 the list writes folder headings with backslashes between the
    components, and list.list_heading_parts() reads them back by splitting on
    both separators. On Linux a backslash is an ordinary filename character -
    unzipping a Windows-made archive produces one routinely - so a directory
    named "Rock\\Metal" is read back as two components. Everything under it
    then resolves into an unrelated folder, or into nothing.
"""

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import tracemalloc
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.test_master_list_generation import MasterListCase  # noqa: E402

BACKSLASH = chr(92)


def code_only(name):
    """`name`'s source with comments and docstrings taken out.

    Both of the statements read below are also described in the comment above
    them and in the changelog entry for them, so a search of the raw source
    would pass on the prose alone.
    """
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        text = handle.read()
    text = re.sub(chr(35) + "[^" + chr(10) + "]*", "", text)
    return re.sub(r'"""..*?"""', "", text, flags=re.S)


class TheCountAgreesWithTheView(unittest.TestCase):
    """#463. The build prints a count; the dashboard's Verify list resolves
    the same collisions to real paths. They have to be the same question, and
    an identifier appearing in a file is not agreement."""

    CORPORA = {
        "one name under two folders": [("A", "Track.flac"), ("B", "Track.flac")],
        "one name under three folders": [("A", "T.flac"), ("B", "T.flac"),
                                         ("C", "T.flac")],
        "the same folder twice is not a collision": [("A", "T.flac"),
                                                     ("A", "T.flac")],
        "case does not separate two copies": [("A", "Track.flac"),
                                              ("B", "TRACK.FLAC")],
        "a folderless row is the library root": [("", "T.flac"), ("A", "T.flac")],
        "a blank name is not a row": [("A", ""), ("B", "  "), ("C", "T.flac")],
        "surrounding space is not part of the name": [("A", " T.flac "),
                                                      ("B", "T.flac")],
        "nothing collides": [("A", "One.flac"), ("B", "Two.flac")],
        "empty": [],
    }

    def test_the_two_answers_are_the_same_number(self):
        for label, corpus in self.CORPORA.items():
            with self.subTest(corpus=label):
                entries = [{"folder": folder, "filename": name}
                           for folder, name in corpus]

                self.assertEqual(
                    list_mod.count_duplicate_filenames(iter(corpus)),
                    len(list_mod.find_duplicate_filenames(entries)),
                    "the build's count and the dashboard's list disagree")

    def test_a_third_folder_does_not_count_the_name_twice(self):
        """The rule is names, not pairs of folders. Worth its own assertion
        because it is the one an incremental counter gets wrong."""
        corpus = [("A", "T.flac"), ("B", "T.flac"), ("C", "T.flac")]

        self.assertEqual(list_mod.count_duplicate_filenames(corpus), 1)

    def test_it_reads_the_rows_once_and_keeps_none_of_them(self):
        """What makes the fix a fix. The caller hands it a generator over the
        scan's own tuples, so anything that needed the input twice - or kept
        it - would put the second copy of the library straight back."""
        rows = iter([("A", "T.flac"), ("B", "T.flac"), ("C", "Other.flac")])

        count = list_mod.count_duplicate_filenames(rows)

        self.assertEqual(count, 1)
        self.assertEqual(list(rows), [], "the iterator was not consumed")

    def test_it_costs_a_fraction_of_what_the_old_shape_did(self):
        """Measured rather than asserted from the shape of the code, and as a
        RATIO against the old approach in the same process - an absolute
        figure would be a different number on every machine and Python.

        The old shape is materialised here deliberately: it is what
        generate_master_list() used to build."""
        rows = [("Folder %04d" % (n // 12), "Track %06d.flac" % n)
                for n in range(20000)]

        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            old = list_mod.find_duplicate_filenames(
                [{"folder": folder, "filename": name} for folder, name in rows])
            _old_peak = tracemalloc.get_traced_memory()[1]
            del old

            tracemalloc.reset_peak()
            new = list_mod.count_duplicate_filenames(iter(rows))
            new_peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        self.assertEqual(new, 0, "fixture invariant: no name collides here")
        self.assertLess(new_peak, _old_peak * 0.6,
                        "the count still costs most of what building the "
                        "whole answer cost: %d vs %d bytes"
                        % (new_peak, _old_peak))


class ABackslashIsNotASeparator(unittest.TestCase):
    """#464."""

    def test_a_component_carrying_one_is_refused(self):
        """The Linux reading, driven on every platform - see the function's
        own note on `separator`. The hazard only exists where the separator is
        "/", and a test that could only run there would be a hole on the
        machine this is being ported to."""
        self.assertTrue(update_list.has_backslash_component(
            "music/Rock" + BACKSLASH + "Metal", separator="/"))

    def test_the_same_text_read_the_windows_way_is_three_folders(self):
        """The other half, and the one that matters for the port: on Windows
        the backslash IS the separator, so the same characters are a perfectly
        ordinary path and no name can contain one. A check that refused this
        would refuse every path on the platform."""
        self.assertFalse(update_list.has_backslash_component(
            "music" + BACKSLASH + "Rock" + BACKSLASH + "Metal",
            separator=BACKSLASH))

    def test_an_ordinary_path_is_not_refused_on_this_machine(self):
        """Unparameterised, so it runs against whatever os.sep actually is
        here: nothing a normal library produces may be excluded."""
        for path in ("music",
                     os.path.join("music", "Rock", "Metal"),
                     os.path.join("music", "Album (2019)"),
                     ""):
            with self.subTest(path=path):
                self.assertFalse(update_list.has_backslash_component(path))

    def test_the_last_component_counts_too(self):
        """A folder whose own name ENDS in a backslash - which is what an
        unzipped Windows path routinely leaves behind."""
        self.assertTrue(update_list.has_backslash_component(
            "music/Odd" + BACKSLASH, separator="/"))

    def test_the_scan_asks_before_it_lists_a_folder(self):
        """The predicate above is only worth anything if the scan consults it,
        and the behaviour test that proves it does cannot run on a filesystem
        where the name is impossible to create. Read as the STATEMENT, on the
        value the scan actually builds."""
        code = code_only("update_list.py")

        self.assertIn("has_backslash_component(rel_dir)", code)

    def test_what_was_left_out_is_said_out_loud(self):
        """An operator comparing the file count against their own expectation
        deserves to know why it is short - the same reasoning denied_dirs
        already carries. Read for the same reason as the test above: the
        behaviour test cannot run on this filesystem."""
        code = code_only("update_list.py")

        self.assertIn("if unlistable_dirs:", code,
                      "folders are dropped from the list with nothing said")

    def test_the_relative_path_is_computed_once_per_directory(self):
        """#463's other half, which has no observable behaviour to assert: the
        rows of one directory share one string object instead of holding a
        separate equal copy each, and the relpath/join calls go from one per
        file to one per directory. Asserted by INDENTATION, because that is
        what "which loop is it in" means here - twelve spaces is the
        per-directory body, twenty is the per-file one."""
        code = code_only("update_list.py")
        statement = "rel_dir = os.path.relpath(root, scan_root)"

        self.assertIn(chr(10) + " " * 12 + statement + chr(10), code,
                      "the relative path is not computed in the per-directory "
                      "loop")
        self.assertEqual(code.count(statement), 1,
                         "it is computed in two places, so one of them is the "
                         "per-file loop it was hoisted out of")

    def test_the_heading_this_protects_would_genuinely_be_misread(self):
        """The reason for the rule, pinned against the reader itself rather
        than described. A folder named with a backslash, written into a
        heading the way update_list.py writes one, comes back as two
        components - and the second one is a folder nobody named."""
        heading = (list_mod.LIST_FOLDER_PREFIX + "music" + BACKSLASH + "Rock"
                   + BACKSLASH + "Metal" + BACKSLASH)

        parts = list_mod.list_heading_parts(heading)

        self.assertEqual(parts, ["music", "Rock", "Metal"],
                         "if this ever stops splitting on the backslash, the "
                         "exclusion in update_list.py can be lifted")


class ARealBuildLeavesOneOut(MasterListCase):
    """#464 end to end, where the platform allows it.

    Windows cannot reach this at all - the backslash IS its separator, so no
    directory can be named with one - and a test that skipped there and did
    nothing else would be a hole on the machine this is being ported to. The
    class above is the half that runs everywhere; this is the half that proves
    the scan actually asks.
    """

    def setUp(self):
        super().setUp()
        self.use_empty_library()

        # PROBED, not assumed from sys.platform. The question is whether THIS
        # filesystem can hold the name at all - and the check has to be what
        # the parent lists, not whether the path exists: os.makedirs() on
        # Windows happily creates "one\two" as two nested directories and
        # os.path.isdir() then answers True for a folder nobody named that.
        parent = os.path.join(self.tree.root, "backslash-probe")
        wanted = "one" + BACKSLASH + "two"
        try:
            os.makedirs(os.path.join(parent, wanted), exist_ok=True)
            held = os.listdir(parent)
        except OSError as err:
            self.skipTest("this filesystem will not hold a backslash in a "
                          "directory name: %s" % err)
        shutil.rmtree(parent, ignore_errors=True)
        if held != [wanted]:
            self.skipTest("the backslash is a separator here, so a folder "
                          "cannot be named with one and the hazard does not "
                          "exist on this platform")

    def build_output(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertTrue(self.generate())
        return buffer.getvalue()

    def test_the_folder_is_left_out_and_said_out_loud(self):
        self.add(os.path.join("Good Album", "01 - Intro.flac"))
        self.add(os.path.join("Rock" + BACKSLASH + "Metal", "02 - Riff.flac"))

        output = self.build_output()
        published = self.read_list()

        self.assertIn("01 - Intro.flac", published)
        self.assertNotIn("02 - Riff.flac", published,
                         "a file was listed under a heading that reads back "
                         "as a different folder")
        self.assertIn("literal backslash", output)
        self.assertIn("1 folder(s) were excluded", output)

    def test_a_clean_library_says_nothing_about_it(self):
        """A line on every build is a line nobody reads."""
        self.add(os.path.join("Good Album", "01 - Intro.flac"))

        self.assertNotIn("literal backslash", self.build_output())

    def test_everything_below_it_goes_too(self):
        """The heading for a subfolder carries the broken component as well,
        so listing the children would advertise the same unreachable path one
        level down."""
        self.add(os.path.join("Rock" + BACKSLASH + "Metal", "Disc 1",
                              "03 - Solo.flac"))

        self.build_output()

        self.assertNotIn("03 - Solo.flac", self.read_list())


class TheTextArtifactStreams(unittest.TestCase):
    """#463's third part. _write_text_artifact() read each member whole, and a
    member IS the list - several hundred megabytes on a library big enough for
    an operator to have chosen the txt format."""

    def copy(self, text, strip):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "member.txt")
            with io.open(source, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            target = os.path.join(folder, "out.txt")
            with io.open(target, "w", encoding="utf-8", newline="\n") as out:
                update_list._copy_removing_first(source, out, strip)
            with io.open(target, encoding="utf-8") as handle:
                return handle.read()

    def test_it_copies_a_member_unchanged_when_there_is_nothing_to_remove(self):
        text = "line one\nline two\n"

        self.assertEqual(self.copy(text, ""), text)

    def test_it_removes_the_banner_at_the_top(self):
        banner = "\n-- operator banner --\n"
        text = "header\n" + banner + "rows\n"

        self.assertEqual(self.copy(text, banner), "header\nrows\n")

    def test_it_removes_only_the_first_one(self):
        """`str.replace(x, "", 1)` is what this replaced, and the count is
        part of the behaviour: a banner-looking line further down is the
        operator's own content."""
        banner = "\n-- operator banner --\n"
        text = "header\n" + banner + "rows\n" + banner + "more\n"

        self.assertEqual(self.copy(text, banner),
                         "header\nrows\n" + banner + "more\n")

    def test_only_the_first_one_goes_even_when_they_are_chunks_apart(self):
        """The single-chunk case above cannot tell the two apart: both copies
        are in the same buffer, and only the first is ever looked for there.
        It takes a second copy in a LATER chunk to prove the search actually
        stops after the first - which is what `replace(x, "", 1)` meant, and
        what a streaming version quietly stops doing."""
        banner = "\n-- operator banner --\n"
        filler = "x" * (update_list._COPY_CHUNK_CHARS * 2)
        text = "header\n" + banner + filler + banner + "tail\n"

        self.assertEqual(self.copy(text, banner),
                         "header\n" + filler + banner + "tail\n")

    def test_a_banner_straddling_a_chunk_boundary_is_still_removed(self):
        """The whole reason the copy carries an overlap. Without it the banner
        is written through, and the result is a list with the operator's
        banner repeated in the middle of it - which is the exact thing the
        removal exists to prevent."""
        banner = "\n-- operator banner --\n"
        chunk = update_list._COPY_CHUNK_CHARS
        head = "h" * (chunk - len(banner) // 2)
        text = head + banner + "rows\n"

        self.assertEqual(self.copy(text, banner), head + "rows\n")

    def test_a_banner_that_is_not_there_leaves_the_member_whole(self):
        """A member written before the operator set a banner, or after they
        cleared it. Nothing may be dropped."""
        chunk = update_list._COPY_CHUNK_CHARS
        text = ("x" * (chunk + 500)) + "\ntail\n"

        self.assertEqual(self.copy(text, "\n-- absent --\n"), text)

    def peak_copying(self, chunks):
        """The traced peak while copying a file `chunks` chunks long."""
        text = "x" * (update_list._COPY_CHUNK_CHARS * chunks)
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "member.txt")
            with io.open(source, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            target = os.path.join(folder, "out.txt")
            tracemalloc.start()
            try:
                tracemalloc.reset_peak()
                with io.open(target, "w", encoding="utf-8", newline="\n") as out:
                    update_list._copy_removing_first(source, out, "")
                return tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()

    def test_the_cost_does_not_grow_with_the_member(self):
        """The property stated as what it actually is: not "small", which is a
        different number on every machine and Python, but FLAT. A member three
        times the size must not cost three times the memory - that is what
        `handle.read()` did, and it is what a later edit would put back.

        Compared against itself in the same process, so nothing here depends
        on the machine it runs on."""
        small = self.peak_copying(2)
        large = self.peak_copying(6)

        self.assertLess(large, small * 1.5,
                        "tripling the file tripled the memory: %d bytes for "
                        "two chunks, %d for six" % (small, large))
