"""generate_master_list() - the scan that produces the file every user downloads.

update_list.py was the last module with no test of its scan path. The stats it
publishes alongside the list are covered by test_list_stats.py; this is the list
itself: what the scan picks up, what the two files contain, that the header the
advert reads agrees with the lines the search reads, and that a failed run leaves
the previous index untouched.

It also pins an invariant that is currently load-bearing and undocumented - see
TheRequestTriggerIsStable.
"""

import os
import sys
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import commands  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import list as list_mod  # noqa: E402
import platform_compat  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class MasterListCase(DCCoreTestCase):
    """A library on disk, and a place to write the lists."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.LOCAL_LIST_DIR = self.tree.lists
        config.FILE_DIRECTORY = self.tree.music
        config.LIST_BASE_NAME = "DCCore"
        config.NICKNAME = "DCCore"
        config.ORIGINAL_NICK = "DCCore"

    def add(self, relative, data=b"\x00" * 2048):
        path = os.path.join(self.tree.music, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def generate(self):
        return update_list.generate_master_list()

    def list_path(self):
        names = [n for n in os.listdir(self.tree.lists)
                 if n.startswith(config.LIST_BASE_NAME)
                 and n.endswith(".txt") and "-RAR-" not in n]
        self.assertEqual(len(names), 1, f"expected one master list, found {names}")
        return os.path.join(self.tree.lists, names[0])

    def rar_path(self):
        names = [n for n in os.listdir(self.tree.lists) if "-RAR-" in n]
        self.assertEqual(len(names), 1, f"expected one album list, found {names}")
        return os.path.join(self.tree.lists, names[0])

    def read_list(self):
        with open(self.list_path(), encoding="utf-8") as handle:
            return handle.read()

    def request_lines(self):
        return [line for line in self.read_list().split("\n") if line.startswith("!")]


class ScanFindsTheRightFiles(MasterListCase):
    """TempTree starts with two .flac tracks in one album folder."""

    def test_the_baseline_library_is_listed(self):
        self.assertTrue(self.generate())
        self.assertEqual(len(self.request_lines()), 2)

    def test_mp3_and_flac_are_both_picked_up(self):
        self.add("Artist/Album/track.mp3")
        self.assertTrue(self.generate())
        names = self.read_list()
        self.assertIn("track.mp3", names)
        self.assertIn("Enter Sandman.flac", names)

    def test_other_files_are_ignored(self):
        """A music library is full of covers, cue sheets and notes."""
        for junk in ("Artist/Album/cover.jpg", "Artist/Album/info.nfo",
                     "Artist/Album/playlist.m3u", "Artist/Album/notes.txt",
                     "Artist/Album/disc.cue"):
            self.add(junk, b"junk")
        self.assertTrue(self.generate())
        listing = self.read_list()
        for junk in ("cover.jpg", "info.nfo", "playlist.m3u", "notes.txt", "disc.cue"):
            with self.subTest(junk=junk):
                self.assertNotIn(junk, listing)
        self.assertEqual(len(self.request_lines()), 2)

    def test_the_extension_test_is_case_insensitive(self):
        """Ripped libraries are full of .FLAC and .Mp3."""
        self.add("Artist/Loud/SHOUTING.FLAC")
        self.add("Artist/Loud/Mixed.Mp3")
        self.assertTrue(self.generate())
        self.assertEqual(len(self.request_lines()), 4)

    def test_it_recurses_all_the_way_down(self):
        self.add("A/B/C/D/E/deep.flac")
        self.assertTrue(self.generate())
        self.assertIn("deep.flac", self.read_list())

    def test_an_empty_library_still_produces_a_list(self):
        empty = os.path.join(self.tree.root, "empty-library")
        os.makedirs(empty, exist_ok=True)
        config.FILE_DIRECTORY = empty
        self.assertTrue(self.generate())
        self.assertEqual(self.request_lines(), [])


class WriterAndReaderAgree(MasterListCase):
    """update_list writes the file; list.py counts it for the advert.

    They are in different modules with no shared constant, so the only thing
    keeping them consistent is that every request line starts with "!" and no
    header does. Worth asserting, because the advert's file count is the number
    users see in channel.
    """

    def test_the_advert_count_matches_the_files_on_disk(self):
        for extra in ("A/one.flac", "A/two.mp3", "B/three.flac"):
            self.add(extra)
        self.assertTrue(self.generate())
        count, _date, _size, _raw = list_mod.get_file_count_date_size_and_raw_bytes()
        self.assertEqual(count, 5, "two from the tree plus three added")
        self.assertEqual(len(self.request_lines()), 5)

    def test_the_header_count_matches_the_request_lines(self):
        self.add("A/extra.flac")
        self.assertTrue(self.generate())
        header = self.read_list().split("\n")[0]
        self.assertIn(f"List of {len(self.request_lines()):,} Files", header)

    def test_no_header_line_is_mistaken_for_a_request(self):
        """The reader counts every line starting with "!"; headers must not."""
        self.assertTrue(self.generate())
        for line in self.read_list().split("\n")[:3]:
            with self.subTest(line=line):
                self.assertFalse(line.startswith("!"),
                                 "a header starting with ! would inflate the advert count")

    def test_the_album_list_is_excluded_from_the_count(self):
        """find_latest_list skips -RAR-; if it did not, albums would be counted twice."""
        self.assertTrue(self.generate())
        self.assertNotIn("-RAR-", os.path.basename(list_mod.find_latest_list()))


class TheRequestTriggerIsStable(MasterListCase):
    """An invariant that currently holds by accident, and is worth keeping.

    The list stamps its request lines with config.NICKNAME. irc.py rebinds
    NICKNAME to ALT_NICKNAME after a 433 collision, and ALT_NICKNAME is
    deliberately NOT one of get_bot_aliases() - so a list stamped with it goes
    dead the moment the bot recovers its main nick.

    Production is safe because handle_list_update_request runs update_list.py as
    a SUBPROCESS, which imports a fresh config where NICKNAME is the file default
    and equals ORIGINAL_NICK, which IS an alias. Nothing states that dependency,
    and "why are we spawning a subprocess for this?" is a very natural cleanup.
    """

    def test_production_generates_the_list_in_a_subprocess(self):
        """That the handler RUNS update_list.py as a subprocess, not that
        commands.py contains the text "subprocess.run".

        The previous version sliced the source from "def
        handle_list_update_request" and asserted the substring appeared
        somewhere after it. A comment saying "we use subprocess.run here"
        passed. So did wrapping the real call in `if False:`.

        What the substring stood for is load-bearing, and this class's own
        docstring explains why: in-process generation would stamp the list
        with the LIVE nick, which after a 433 collision is the alt nick, and
        the alt nick is not one of the aliases the bot answers to. The list
        would be built and then be unrequestable.
        """
        import subprocess
        import time as time_mod
        import types

        calls = []
        real_run = subprocess.run

        def recorder(args, **kwargs):
            calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        subprocess.run = recorder
        self.addCleanup(setattr, subprocess, "run", real_run)

        commands.handle_list_update_request("operator", "#channel", authorised=True)

        # The handler dispatches into a daemon thread and returns immediately.
        # Polled rather than joined: a thread-set diff also catches whatever
        # unrelated daemon threads the rest of the suite has running, and
        # joining one of those waits out the whole timeout for nothing.
        deadline = time_mod.time() + 10
        while not calls and time_mod.time() < deadline:
            time_mod.sleep(0.01)

        self.assertTrue(calls, "the list was not rebuilt in a subprocess")
        launched = [a for a in calls
                    if any("update_list.py" in str(part) for part in a)]
        self.assertTrue(launched, f"a subprocess ran, but not update_list.py: {calls}")

    def test_the_default_nick_is_an_alias_so_lists_keep_working(self):
        config.NICKNAME = "DCCore_"          # after a 433 collision
        config.ORIGINAL_NICK = "DCCore"
        self.assertIn("dccore", irc.get_bot_aliases(),
                      "a list stamped with the config default must stay requestable")

    def test_the_alt_nick_is_not_an_alias(self):
        """Which is why the subprocess matters: it never stamps the alt nick."""
        config.NICKNAME = "DCCore"
        config.ORIGINAL_NICK = "DCCore"
        config.ALT_NICKNAME = "DCCore_"
        self.assertNotIn("dccore_", irc.get_bot_aliases())

    def test_in_process_generation_stamps_whatever_nick_is_live(self):
        """Documents the hazard rather than asserting it is fine.

        If this ever becomes the production path, every line of the published
        list is stamped with a trigger the bot stops answering to on recovery.
        """
        config.NICKNAME = "DCCore_"
        self.assertTrue(self.generate())
        self.assertTrue(all(line.startswith("!DCCore_ ") for line in self.request_lines()),
                        "in-process generation follows the live nick - hence the subprocess")


class TheAlbumList(MasterListCase):
    def test_one_line_per_album_folder(self):
        self.add("Metallica/Ride The Lightning (1984)/01.flac")
        self.add("Metallica/Ride The Lightning (1984)/02.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_lines = [l for l in handle.read().split("\n") if l.startswith("!")]
        self.assertEqual(len(rar_lines), len(set(rar_lines)),
                         "an album must not be advertised twice")

    def test_a_multidisc_album_is_advertised_once(self):
        """What written_rar_folders is actually for.

        Two files in ONE folder are already deduplicated by the folder-change
        check. The set only earns its keep when two DIFFERENT folders clean down
        to the same album root - which is exactly what CD1/CD2 do.
        """
        self.add("Metallica/Black Album/CD1/01.flac")
        self.add("Metallica/Black Album/CD2/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_lines = [l for l in handle.read().split("\n") if l.startswith("!")]
        self.assertEqual(len(rar_lines), len(set(rar_lines)),
                         "a multidisc album must not appear twice in the !rar list")

    def test_it_names_the_rar_trigger(self):
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            self.assertIn("!rar", handle.read())

    def test_multidisc_truncation_still_works(self):
        """Defect guard for the segment-anchored rewrite: the ordinary
        "CD1"/"CD2" case must keep working exactly as before."""
        self.add("Metallica/Black Album/CD1/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        self.assertIn("Black Album", rar_text)
        self.assertNotIn("CD1", rar_text,
                         "the disc subfolder must still be stripped from the row")

    def test_discography_is_not_mistaken_for_disc(self):
        """#162 finding #16, the headline repro: a substring match let
        "\\disc" fire on "\\Discography", collapsing the row to the ARTIST
        root - which dcc.py refuses outright - and leaving the real album
        with no requestable row at all (written_rar_folders deduplicates
        on the wrong, truncated string)."""
        self.add("Pink Floyd/Discography 1967-2014/The Wall/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        self.assertIn("The Wall", rar_text,
                     "the specific album must still be offered, not collapsed "
                     "to the artist root")
        # The old bug's own row - the refused artist root alone - must not
        # appear as a substitute for the real one.
        rar_lines = [l for l in rar_text.split("\n") if l.startswith("!")]
        self.assertNotIn("!DCCore !rar D:\\MUSIC\\Pink Floyd\\", rar_lines)

    def test_a_media_markt_style_folder_is_not_mistaken_for_media(self):
        """The same substring bug, the "\\media" box word this time."""
        self.add("Various Artists/Media Markt Hits/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        self.assertIn("Media Markt Hits", rar_text)

    def test_a_genre_first_library_still_offers_the_specific_album(self):
        """The audit's own 'aggravating variant': on a genre-first layout
        the truncated row had exactly two segments and was ACCEPTED by
        dcc.py, packing the whole discography instead of the one album -
        worse than the plain artist-root case, because nothing about it
        looked refused."""
        self.add("Rock/Pink Floyd/Discography/The Wall/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        self.assertIn("The Wall", rar_text)
        rar_lines = [l for l in rar_text.split("\n") if l.startswith("!")]
        self.assertNotIn("!DCCore !rar D:\\MUSIC\\Rock\\Pink Floyd\\", rar_lines,
                         "must not collapse to the whole-discography folder")

    def test_truncation_never_collapses_to_the_artist_root(self):
        """A folder shaped like "Artist/CD1" (no album subfolder at all)
        would collapse to the single-segment artist root if truncated -
        exactly what dcc.py refuses. Must be left untruncated instead."""
        self.add("SomeArtist/CD1/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        rar_lines = [l for l in rar_text.split("\n") if l.startswith("!")]
        self.assertNotIn("!DCCore !rar D:\\MUSIC\\SomeArtist\\", rar_lines,
                         "must never offer the bare artist root")

    def test_earliest_matching_segment_wins_not_list_order(self):
        """Defect: the old code tried box words in a fixed LIST order
        ('cd' before 'disc') and truncated at whichever one it found
        first IN THAT LIST, anywhere in the string - not whichever one
        appears earliest in the actual path. A folder with "Disc 1" before
        a later "CD 2" used to truncate at the LATER "CD 2" (because "cd"
        is checked first), keeping the wrong, more specific-looking folder
        instead of the actual, earlier album boundary."""
        self.add("Artist/Album/Disc 1/CD 2/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        rar_lines = [l for l in rar_text.split("\n") if l.startswith("!")]
        # "Disc 1" is the earliest matching segment (index 2) - truncating
        # there leaves "Artist/Album" (two segments, the real album).
        self.assertIn("!DCCore !rar D:\\MUSIC\\Artist\\Album\\", rar_lines)
        self.assertNotIn("!DCCore !rar D:\\MUSIC\\Artist\\Album\\Disc 1\\", rar_lines,
                         "must not stop at the later 'CD' match instead of "
                         "the earlier 'Disc' one")


class PruningSupersededLists(MasterListCase):
    """_prune_superseded_lists deletes files, so what it spares matters."""

    def touch(self, name, body="x"):
        path = os.path.join(self.tree.lists, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def test_yesterdays_lists_are_removed(self):
        old = self.touch(f"{config.LIST_BASE_NAME}-2020-01-01.txt")
        old_zip = self.touch(f"{config.LIST_BASE_NAME}-2020-01-01.zip")
        self.assertTrue(self.generate())
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(old_zip))

    def test_todays_new_lists_are_kept(self):
        self.assertTrue(self.generate())
        self.assertTrue(os.path.exists(self.list_path()))
        self.assertTrue(os.path.exists(self.rar_path()))

    def test_unrelated_files_are_never_touched(self):
        """The list directory is a real directory someone may keep things in."""
        keepers = [self.touch("notes.txt"), self.touch("OtherBot-2020-01-01.txt"),
                   self.touch("readme.md"), self.touch("archive.tar.gz")]
        self.assertTrue(self.generate())
        for path in keepers:
            with self.subTest(path=os.path.basename(path)):
                self.assertTrue(os.path.exists(path),
                                "pruning must only remove this bot's own old lists")

    def test_a_file_sharing_the_bots_name_but_not_a_list_is_spared(self):
        """The case the extension check exists for.

        The "unrelated files" test above is spared by the startswith check alone,
        so it never exercised this. What needs the extension test is a file that
        DOES begin with LIST_BASE_NAME and is not a generated list - a log, a
        backup, notes the operator keeps beside the lists.
        """
        keepers = [self.touch(f"{config.LIST_BASE_NAME}.log"),
                   self.touch(f"{config.LIST_BASE_NAME}-notes.md"),
                   self.touch(f"{config.LIST_BASE_NAME}-backup.tar.gz"),
                   self.touch(f"{config.LIST_BASE_NAME}-2020-01-01.txt.bak")]
        self.assertTrue(self.generate())
        for path in keepers:
            with self.subTest(path=os.path.basename(path)):
                self.assertTrue(os.path.exists(path),
                                "pruning must only remove .txt and .zip lists")

    def test_the_side_files_are_not_pruned(self):
        """They do not end in .txt by accident of naming - check anyway."""
        self.assertTrue(self.generate())
        self.assertTrue(os.path.exists(list_mod.size_file_path()))
        self.assertTrue(os.path.exists(list_mod.rawbytes_file_path()))


class TheSwapLeavesNothingBehind(MasterListCase):
    def test_no_temp_files_survive_a_successful_run(self):
        self.assertTrue(self.generate())
        leftovers = [n for n in os.listdir(self.tree.lists) if n.endswith(".new")]
        self.assertEqual(leftovers, [])

    def test_no_temp_files_survive_a_refused_run(self):
        """A scan finding nothing next to a good index is refused; clean up after."""
        self.assertTrue(self.generate())
        empty = os.path.join(self.tree.root, "vanished")
        os.makedirs(empty, exist_ok=True)
        config.FILE_DIRECTORY = empty
        self.assertFalse(self.generate())
        leftovers = [n for n in os.listdir(self.tree.lists) if n.endswith(".new")]
        self.assertEqual(leftovers, [], "a refused run must not litter the list directory")

    def test_the_zip_holds_the_final_names_not_the_temp_ones(self):
        """Users open this archive; ".txt.new" inside it would be visible."""
        self.assertTrue(self.generate())
        zip_name = [n for n in os.listdir(self.tree.lists) if n.endswith(".zip")][0]
        with zipfile.ZipFile(os.path.join(self.tree.lists, zip_name)) as archive:
            names = archive.namelist()
        self.assertTrue(names)
        for name in names:
            with self.subTest(name=name):
                self.assertFalse(name.endswith(".new"))


class UnreadableSubtreeKeepsThePreviousIndex(MasterListCase):
    """#162 finding #15: os.walk()'s default onerror=None silently skips a
    subtree it cannot read (a stale NFS handle, EIO, a revoked ACL) - the
    scan's own file count comes out non-zero regardless, so the "0 files
    found" guard never catches it, and a TRUNCATED index gets published
    over a good one with no error anywhere.

    A real unreadable directory needs real broken permissions, which chmod
    cannot portably simulate in CI (Windows, and this suite runs as root in
    some environments) - so os.walk() is stubbed to call its own onerror
    callback, exactly as a genuinely unreadable subtree would, without
    touching real filesystem permissions at all."""

    def walk_erroring(self, err):
        real_walk = os.walk

        def fake_walk(top, *args, **kwargs):
            onerror = kwargs.get("onerror")
            if onerror is not None:
                onerror(err)
            yield from real_walk(top, *args, **kwargs)

        os.walk = fake_walk
        self.addCleanup(lambda: setattr(os, "walk", real_walk))

    def test_a_walk_error_refuses_to_publish(self):
        self.add("Metallica/Black Album/01.flac")
        self.assertTrue(self.generate())
        old_list = self.read_list()

        # Would change the published list's contents if the run went
        # through - proves the refusal, not just an unlucky no-op.
        self.add("Metallica/New Album/02.flac")
        self.walk_erroring(OSError(5, "Input/output error", "/some/unreadable/subtree"))

        result = self.generate()

        self.assertFalse(result)
        self.assertEqual(self.read_list(), old_list,
                         "a partial scan must never overwrite a good index")

    def test_no_walk_error_still_publishes_normally(self):
        """Control: the onerror wiring itself must not refuse a clean scan."""
        self.add("Metallica/Black Album/01.flac")
        self.assertTrue(self.generate())


class AnUnreadableFileIsExcludedNotPublishedAsZeroBytes(MasterListCase):
    """#228: a bare `except: pass` around os.path.getsize() left file_bytes
    at 0 and still appended the entry - permission denied, a dangling
    symlink, or a file removed mid-scan all read as a legitimate 0-byte
    track. The list is what the bot HANDS OUT: publishing it offered a
    download that can only ever fail once someone actually requests it, with
    the library's reported total size quietly short and no log line saying
    why.

    Deliberately the opposite fix from UnreadableSubtreeKeepsThePreviousIndex
    above: THAT is a whole subtree going unreadable, systemic enough to
    refuse the whole run and keep the previous good index. ONE file failing
    getsize() is not - excluding just that file and publishing everything
    else is what the control below checks was not lost along with the bug.
    """

    def getsize_raising_for(self, target_path):
        real_getsize = os.path.getsize

        def fake_getsize(path):
            # Compared through long_path() on BOTH sides, because that is how
            # the code under test calls it: update_list.py:413 is
            # os.path.getsize(platform_compat.long_path(full_file_path)).
            #
            # On Windows long_path() prefixes "\?\", so a plain normpath
            # comparison never matched, this stub never raised, and both tests
            # below silently exercised the READABLE path - passing on Linux,
            # where long_path() is the identity function, and failing here.
            #
            # A stub has to match the way production calls the function, not
            # the way the test happens to hold the path.
            if (platform_compat.long_path(os.path.normpath(path))
                    == platform_compat.long_path(os.path.normpath(target_path))):
                raise OSError(13, "Permission denied", path)
            return real_getsize(path)

        os.path.getsize = fake_getsize
        self.addCleanup(lambda: setattr(os.path, "getsize", real_getsize))

    def test_the_unreadable_file_is_left_out(self):
        good_path = self.add("Metallica/Black Album/01.flac")
        bad_path = self.add("Metallica/Black Album/02.flac")
        self.getsize_raising_for(bad_path)

        self.assertTrue(self.generate(),
                        "one bad file must not refuse the whole scan")

        lines = self.request_lines()
        self.assertTrue(any("01.flac" in line for line in lines),
                        "the readable file must still be published")
        self.assertFalse(any("02.flac" in line for line in lines),
                         "the unreadable file must not be published as a "
                         "0-byte entry nobody can ever actually download")

    def test_the_readable_files_total_size_is_not_shorted(self):
        """A second control: the excluded file's phantom 0 bytes must not
        even implicitly participate - the header's total is exactly what the
        readable files (the tree's two defaults plus the one added here) add
        up to, not counting the excluded file at all."""
        good_path = self.add("Metallica/New Album/01.flac", data=b"\x00" * 4096)
        bad_path = self.add("Metallica/New Album/02.flac", data=b"\x00" * 4096)
        self.getsize_raising_for(bad_path)

        self.generate()

        header = self.read_list().split("\n", 1)[0]
        expected_files = len(self.tree.tracks) + 1  # the tree's two defaults, plus the good one
        self.assertIn(f"List of {expected_files} Files", header)


class SideFilesOnlyPublishAfterTheSwap(MasterListCase):
    """#162 finding #32: the size/rawbytes side files used to be written
    BEFORE the os.replace() calls that actually publish the new list, so a
    failed swap rolled the list back to the previous index while the side
    files had already been overwritten with the new (unpublished) scan's
    numbers - an old index wearing a new scan's size."""

    def _side_file_contents(self):
        size_path = os.path.join(self.tree.lists, config.LIST_SIZE_FILE)
        rawbytes_path = os.path.join(self.tree.lists, config.LIST_RAWBYTES_FILE)
        with open(size_path, encoding="utf-8") as handle:
            size = handle.read()
        with open(rawbytes_path, encoding="utf-8") as handle:
            rawbytes = handle.read()
        return size, rawbytes

    def test_a_failed_swap_never_touches_the_side_files(self):
        self.add("Metallica/Black Album/01.flac")
        self.assertTrue(self.generate())
        old_side_files = self._side_file_contents()
        old_list = self.read_list()

        # A bigger scan, so the side files WOULD visibly change if this
        # reached the write - proves the ordering, not an unlucky no-op.
        self.add("Metallica/New Album/02.flac", data=b"\x00" * 999999)

        real_replace = os.replace

        def failing_replace(src, dst):
            raise OSError("simulated replace failure")

        os.replace = failing_replace
        self.addCleanup(lambda: setattr(os, "replace", real_replace))

        result = self.generate()

        self.assertFalse(result)
        self.assertEqual(self.read_list(), old_list,
                         "the previous index must survive a failed swap")
        self.assertEqual(self._side_file_contents(), old_side_files,
                         "the side files must never run ahead of a swap "
                         "that never actually happened")


class DiscardingTempLists(MasterListCase):
    """_discard_temp_lists is the cleanup every refusal path calls.

    One of its three call sites - the "no list was written" guard - is defensive
    and not reachable from a normal run, so the helper is tested directly rather
    than left to whichever branch happens to be exercised.
    """

    def test_it_removes_what_is_there(self):
        paths = []
        for name in ("a.txt.new", "b.txt.new"):
            path = os.path.join(self.tree.lists, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("half written")
            paths.append(path)
        update_list._discard_temp_lists(*paths)
        for path in paths:
            with self.subTest(path=os.path.basename(path)):
                self.assertFalse(os.path.exists(path))

    def test_a_missing_file_is_not_an_error(self):
        """Called on paths that may never have been created."""
        update_list._discard_temp_lists(
            os.path.join(self.tree.lists, "never-existed.new"))

    def test_none_and_empty_paths_are_tolerated(self):
        update_list._discard_temp_lists(None, "", None)

    def test_a_file_it_cannot_remove_does_not_raise(self):
        """Cleanup runs on the failure path; it must not raise a second failure."""
        real_remove = os.remove

        def refuse(path):
            raise OSError(13, "Permission denied")

        path = os.path.join(self.tree.lists, "locked.txt.new")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.remove = refuse
        try:
            update_list._discard_temp_lists(path)
        finally:
            os.remove = real_remove
        self.assertTrue(os.path.exists(path), "the stub refused, as intended")


class AwkwardFilenames(MasterListCase):
    """A real library is not made of ASCII."""

    def test_an_apostrophe_survives(self):
        self.add("Artist/Album/A Winter's Tale.flac")
        self.assertTrue(self.generate())
        self.assertIn("A Winter's Tale.flac", self.read_list())

    def test_non_ascii_survives(self):
        self.add("Björk/Homogenic/Jóga.flac")
        self.assertTrue(self.generate())
        self.assertIn("Jóga.flac", self.read_list())

    def test_a_filename_with_spaces_stays_on_one_line(self):
        self.add("Artist/Album/A Very Long Track Name Indeed.flac")
        self.assertTrue(self.generate())
        matches = [l for l in self.request_lines()
                   if "A Very Long Track Name Indeed.flac" in l]
        self.assertEqual(len(matches), 1)

    def test_a_filename_cannot_split_a_request_line(self):
        """Found by CI on Linux; Windows refuses such a name at creation.

        POSIX allows newlines in filenames - only "/" and NUL are forbidden - so
        this is a legal track on the box the daemon runs on. Written verbatim it
        turned one entry into two lines, leaving a truncated entry and an orphan
        fragment, and the list was malformed from there down.
        """
        try:
            self.add("Artist/Album/evil\nname.flac")
        except (OSError, ValueError):
            self.skipTest("this filesystem refuses newlines in filenames")
        self.assertTrue(self.generate())
        lines = self.request_lines()
        self.assertEqual(len(lines), 3, "two real tracks plus the awkward one")
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(line.count("::INFO::"), 1,
                                 "every request line carries exactly one size marker")


class FlatteningIsWiredIntoTheWriter(MasterListCase):
    """That _one_line exists is not the same as the writer using it.

    The filesystem test can only run where a newline is a legal filename, so on
    Windows it skips and the wiring goes unverified - a mutation removing the
    call from the request line survives there. Stubbing the walk instead of the
    filesystem tests the same path on every platform.
    """

    def walk_yielding(self, *names):
        """Make the scan see `names` in the album folder, whatever is on disk.

        The names this class yields (embedded newlines, surrogateescape
        bytes) generally do not exist as real files - only os.walk() is
        stubbed to report them, deliberately, so the test works on every
        platform including one where such a name cannot actually be created.
        os.path.getsize() is therefore also stubbed here: since #228,
        generate_master_list() excludes a file it cannot read the size of,
        and unwrapped every synthetic name here would raise
        FileNotFoundError and be silently excluded - passing every assertion
        below by leaving nothing to make them fail, not by the flattening
        actually working.
        """
        real_walk = os.walk
        real_getsize = os.path.getsize
        album = os.path.abspath(self.tree.album)

        def fake_walk(top, *args, **kwargs):
            for root, dirs, files in real_walk(top, *args, **kwargs):
                # generate_master_list() now calls os.walk() against a
                # platform_compat.long_path()-wrapped root (#162 finding
                # #21), which on Windows prefixes every yielded `root`
                # with "\\?\" - os.path.abspath() does not strip that, so
                # comparing against the unwrapped `album` below would
                # never match again without stripping it here too.
                root_compare = os.path.abspath(root)
                if root_compare.startswith("\\\\?\\"):
                    root_compare = root_compare[4:]
                if root_compare == album:
                    yield root, dirs, list(names)
                else:
                    yield root, dirs, files

        def fake_getsize(path):
            try:
                return real_getsize(path)
            except OSError:
                return 2048  # matches MasterListCase.add()'s own default size

        os.walk = fake_walk
        os.path.getsize = fake_getsize
        self.addCleanup(lambda: setattr(os, "walk", real_walk))
        self.addCleanup(lambda: setattr(os.path, "getsize", real_getsize))

    def test_a_newline_in_a_scanned_name_cannot_split_the_entry(self):
        self.walk_yielding("evil\nname.flac")
        self.assertTrue(self.generate())
        lines = self.request_lines()
        self.assertEqual(len(lines), 1, "one file scanned, one request line")
        self.assertEqual(lines[0].count("::INFO::"), 1)
        self.assertIn("evil name.flac", lines[0])

    def test_a_carriage_return_cannot_split_the_entry(self):
        self.walk_yielding("cr\rname.flac")
        self.assertTrue(self.generate())
        self.assertEqual(len(self.request_lines()), 1)

    def test_every_line_of_the_published_list_is_intact(self):
        """The property that matters: no orphan fragments anywhere in the file."""
        self.walk_yielding("a\nb.flac", "c\rd.mp3", "normal.flac")
        self.assertTrue(self.generate())
        for line in self.read_list().split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("="):
                continue
            if stripped.startswith("!"):
                with self.subTest(line=stripped):
                    self.assertEqual(stripped.count("::INFO::"), 1)
            else:
                with self.subTest(line=stripped):
                    self.assertFalse("::INFO::" in stripped,
                                     "a size marker outside a request line is an orphan")

    def test_a_non_utf8_filename_does_not_abort_the_whole_rebuild(self):
        """#162 finding #4, the filesystem-independent half: the exact
        surrogateescape shape os.walk() hands back for a non-UTF-8 name on
        a real library, exercised through the real writer - not just
        _one_line() in isolation."""
        self.walk_yielding("Bj\udcf6rk.flac", "normal.flac")
        self.assertTrue(self.generate())
        lines = self.request_lines()
        self.assertEqual(len(lines), 2, "both files must survive the scan")
        self.assertIn("normal.flac", self.read_list())


class FlatteningNamesForOneLinePerEntry(unittest.TestCase):
    """_one_line, tested directly so BOTH platforms verify it.

    The filesystem test above can only run where such a filename can be created,
    which is not Windows - so on the Windows jobs it skips and the fix would go
    unverified. These do not touch the filesystem.
    """

    def test_a_newline_becomes_a_space(self):
        self.assertEqual(update_list._one_line("evil\nname.flac"), "evil name.flac")

    def test_a_carriage_return_becomes_a_space(self):
        self.assertEqual(update_list._one_line("cr\rname.flac"), "cr name.flac")

    def test_a_tab_becomes_a_space(self):
        """The list is column-aligned by spaces; a tab would break the layout."""
        self.assertEqual(update_list._one_line("tab\there.flac"), "tab here.flac")

    def test_other_control_characters_go_too(self):
        for code in (0x00, 0x07, 0x1b, 0x7f):
            with self.subTest(code=hex(code)):
                flattened = update_list._one_line(f"a{chr(code)}b.flac")
                self.assertEqual(flattened, "a b.flac")

    def test_ordinary_names_are_returned_unchanged(self):
        for name in ("Enter Sandman.flac", "A Winter's Tale.flac", "track (1984).mp3"):
            with self.subTest(name=name):
                self.assertEqual(update_list._one_line(name), name)

    def test_non_ascii_is_preserved(self):
        """Flattening must not turn a music library into mojibake."""
        for name in ("Jóga.flac", "Björk.mp3", "Sigur Rós - Svefn-g-englar.flac"):
            with self.subTest(name=name):
                self.assertEqual(update_list._one_line(name), name)

    def test_the_result_never_contains_a_line_break(self):
        """The property the whole helper exists for."""
        for name in ("a\nb", "a\r\nb", "\n\n\n", "a\rb\nc"):
            with self.subTest(name=name):
                flattened = update_list._one_line(name)
                self.assertNotIn("\n", flattened)
                self.assertNotIn("\r", flattened)

    def test_a_non_utf8_byte_does_not_raise(self):
        """#162 finding #4. os.walk() on POSIX decodes a filename with
        non-UTF-8 bytes (a CP1252 rip, a bad extraction, a FAT copy) using
        the "surrogateescape" error handler - a lone surrogate codepoint,
        not itself a control character, but not valid UTF-8 either. The
        exact reported repro: '\\udcf6' embedded in an otherwise normal
        name. Before this fix, writing it with a strict UTF-8 encoder
        raised UnicodeEncodeError and abandoned the entire list rebuild."""
        flattened = update_list._one_line("Bj\udcf6rk.flac")
        # Must not raise, and the result must itself be safely UTF-8
        # encodable - the whole point, since this text is about to be
        # written with io.open(..., encoding="utf-8").
        flattened.encode("utf-8")

    def test_a_non_utf8_byte_becomes_a_visible_placeholder_not_silence(self):
        flattened = update_list._one_line("Bj\udcf6rk.flac")
        self.assertNotIn("\udcf6", flattened)
        self.assertIn("Bj", flattened)
        self.assertIn("rk.flac", flattened)

    def test_valid_unicode_is_unaffected_by_the_utf8_sanitisation(self):
        """The sanitisation step must not itself mangle a perfectly good
        name - it is a round-trip through UTF-8, and every one of these
        already IS valid UTF-8."""
        for name in ("Jóga.flac", "Björk.mp3", "Sigur Rós - Svefn-g-englar.flac"):
            with self.subTest(name=name):
                self.assertEqual(update_list._one_line(name), name)


if __name__ == "__main__":
    unittest.main()


class TheFolderRuleMatchesTheFolder(MasterListCase):
    """The ==== rule above and below each folder header used to be a fixed 53
    characters, while the header it framed was never that width.

    Measured against the operator's real 1.21TB library: 4,107 folder headers,
    running 54 to 136 characters and averaging 80. Every single one was wider
    than the rule, so the framing was always ragged.
    """

    def _blocks(self):
        """Every (rule, header, rule) triple in the generated text list."""
        self.assertTrue(update_list.generate_master_list())
        path = list_mod.find_latest_list()
        self.assertIsNotNone(path, "no list was generated")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = [l.rstrip("\n") for l in handle]

        found = []
        for i in range(1, len(lines) - 1):
            above, header, below = lines[i - 1], lines[i], lines[i + 1]
            if (above and below and set(above) == {"="} and set(below) == {"="}
                    and set(header) != {"="}):
                found.append((above, header, below))
        return found

    def test_every_rule_is_exactly_as_wide_as_its_folder_line(self):
        blocks = self._blocks()
        self.assertTrue(blocks, "the fixture produced no folder headers")
        for above, header, below in blocks:
            self.assertEqual(len(above), len(header),
                             f"rule above is {len(above)}, header is {len(header)}")
            self.assertEqual(len(below), len(header),
                             f"rule below is {len(below)}, header is {len(header)}")

    def test_the_rules_are_not_the_old_fixed_width(self):
        """Guards the specific regression: a 53-character rule against a
        header that is not 53 characters."""
        for above, header, _below in self._blocks():
            if len(header) != 53:
                self.assertNotEqual(
                    len(above), 53,
                    "the rule is back to the old fixed 53 characters")

    def test_a_long_folder_name_still_gets_a_matching_rule(self):
        """The real library's widest header is 136 characters."""
        deep = os.path.join(self.tree.music,
                            "An Artist With A Considerably Longer Name Than Usual",
                            "1997 - An Album Title That Also Runs Long [REMASTER] [16-44]")
        os.makedirs(deep, exist_ok=True)
        with open(os.path.join(deep, "01 - Track.flac"), "wb") as handle:
            handle.write(b"\x00" * 2048)

        widest = max(self._blocks(), key=lambda b: len(b[1]))
        self.assertGreater(len(widest[1]), 53,
                           "fixture did not produce a header wider than the old rule")
        self.assertEqual(len(widest[0]), len(widest[1]))
        self.assertEqual(len(widest[2]), len(widest[1]))
