"""list_fetch.py - safe extraction and parsing of another bot's fetched
master-list zip.

Covers, per the brief: a zip containing a path-traversal entry is rejected
without writing anything outside the intended extraction directory; a zip
whose declared uncompressed size exceeds the sane cap is rejected before
extracting anything; a zip with no recognisable list file inside is handled
gracefully (no crash, a clear (False, reason) result); an ambiguous zip (more
than one candidate .txt) picks the largest and does not crash; and the happy
path parses real entries via list.py's existing pipeline.

Deliberately stdlib-only, like the rest of the suite.
"""

import io
import os
import struct
import sys
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import db  # noqa: E402
import list as list_module  # noqa: E402
import list_fetch  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def _zip_bytes(members):
    """members: [(arcname, data_bytes_or_str), ...]. Returns raw zip bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in members:
            if isinstance(data, str):
                data = data.encode("utf-8")
            zf.writestr(arcname, data)
    return buf.getvalue()


def _write_zip(path, members):
    with open(path, "wb") as f:
        f.write(_zip_bytes(members))


def _read_all(entry):
    """Test helper: re-parse a whole stored entry via the same on-demand
    reader webserver.py uses, with a limit high enough that it never actually
    clips anything in this test suite's small fixtures - a stand-in for "give
    me every row" in tests written before pagination existed, so most of them
    did not need to change shape just to keep testing what they always
    tested. Returns the row list alone; callers that care about total/error
    call list_fetch.get_fetched_bot_page() directly instead."""
    # Paged by FOLDER now, so flatten the groups back to a flat row list -
    # what every caller of this helper is actually asserting about.
    groups, _folders, _rows, error = list_fetch.get_fetched_bot_page(
        entry, 0, 10**9)
    rows = [row for group in groups for row in group["entries"]]
    assert error is None, f"unexpected read error: {error}"
    return rows


def _list_txt_folders(base_name, folders):
    """A master list spanning SEVERAL folders.

    _list_txt() below writes one folder, which is all the extraction tests
    ever needed. Paging is by folder now, so a one-folder list exercises
    nothing - these tests need a list with folders to page over.
    """
    rule = "=" * 53
    lines = [
        "List of N Files generated on Jan 1st\n",
        f"To request a file, copy/paste to the channel... !{base_name} FILENAME\n\n\n",
    ]
    for folder, files in folders:
        lines.append("\n" + rule + "\n")
        lines.append(folder + "\n")
        lines.append(rule + "\n")
        for filename, size in files:
            lines.append(f"!{base_name} {filename}  ::INFO:: {size}\n")
    return "".join(lines)


def _list_txt(base_name="OtherBot", files=(("Track One.flac", "10.0MB"),)):
    lines = [
        "List of 1 Files (10.0MB) generated on Jan 1st\n",
        f"To request a file, copy/paste to the channel... !{base_name} FILENAME\n\n\n",
        "\n" + "=" * 53 + "\n",
        "D:\\MUSIC\\SomeAlbum\\\n",
        "=" * 53 + "\n",
    ]
    for filename, size in files:
        lines.append(f"!{base_name} {filename}  ::INFO:: {size}\n")
    return "".join(lines)


class SafeExtractionTests(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-listfetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.zip_path = os.path.join(self.tmp, "incoming.zip")

    def test_happy_path_extracts_and_parses(self):
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt())])

        ok, reason = list_fetch.process_fetched_list_zip("otherbot", self.zip_path)

        self.assertTrue(ok, reason)
        self.assertIsNone(reason)
        entry = config.fetched_bot_lists["otherbot"]
        self.assertEqual(entry["bot"], "otherbot")
        # Issue #76, option 2: no "entries" key at all - a path and a
        # precomputed count instead, so nothing here is retained in memory.
        self.assertNotIn("entries", entry)
        self.assertTrue(entry["list_path"])
        self.assertTrue(os.path.exists(entry["list_path"]),
                         "the extracted .txt must still be on disk for later, "
                         "on-demand reads to work")
        self.assertEqual(entry["entry_count"], 1)

        rows = _read_all(entry)
        titles = [row["title"] for row in rows]
        self.assertEqual(titles, ["Track One.flac"])
        self.assertEqual(rows[0]["size"], "10.0MB")
        self.assertEqual(rows[0]["source"], "otherbot")

    def test_a_second_fetch_for_the_same_bot_replaces_the_first(self):
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt(files=(("First.flac", "1.0MB"),)))])
        list_fetch.process_fetched_list_zip("otherbot", self.zip_path)

        _write_zip(self.zip_path, [("OtherBot-2026-08-28.txt", _list_txt(files=(("Second.flac", "2.0MB"),)))])
        list_fetch.process_fetched_list_zip("otherbot", self.zip_path)

        self.assertEqual(len(config.fetched_bot_lists), 1)
        entry = config.fetched_bot_lists["otherbot"]
        self.assertEqual(entry["entry_count"], 1)
        titles = [row["title"] for row in _read_all(entry)]
        self.assertEqual(titles, ["Second.flac"])

    def test_bot_nick_is_case_insensitively_keyed_but_preserves_display_case(self):
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt())])
        list_fetch.process_fetched_list_zip("OtherBot", self.zip_path)

        self.assertIn("otherbot", config.fetched_bot_lists)
        self.assertEqual(config.fetched_bot_lists["otherbot"]["bot"], "OtherBot")

    # ---------------------------------------------------------- Path traversal

    def test_a_relative_path_traversal_entry_is_rejected_and_nothing_is_written_outside(self):
        _write_zip(self.zip_path, [("../../evil.txt", "pwned")])

        ok, reason = list_fetch.process_fetched_list_zip("evilbot", self.zip_path)

        self.assertFalse(ok)
        self.assertIn("traversal", reason.lower())
        self.assertNotIn("evilbot", config.fetched_bot_lists)
        # Nothing was left anywhere under FETCHED_FILES_DIR, including
        # outside the per-bot extraction directory - the whole archive is
        # rejected before any member is ever written, not just the
        # offending one.
        for root, _dirs, files in os.walk(self.tmp):
            self.assertNotIn("evil.txt", files)

    def test_an_absolute_path_entry_is_rejected(self):
        _write_zip(self.zip_path, [("/etc/cron.d/evil", "pwned")])

        ok, reason = list_fetch.process_fetched_list_zip("evilbot", self.zip_path)

        self.assertFalse(ok)
        self.assertIn("absolute", reason.lower())
        self.assertFalse(os.path.exists("/etc/cron.d/evil"))

    def test_a_windows_drive_letter_absolute_path_entry_is_rejected(self):
        _write_zip(self.zip_path, [("C:/Windows/evil.txt", "pwned")])

        ok, reason = list_fetch.process_fetched_list_zip("evilbot", self.zip_path)

        self.assertFalse(ok)
        self.assertIn("absolute", reason.lower())

    def test_extraction_directory_is_left_clean_after_a_rejection(self):
        extract_dir = list_fetch.list_extract_dir("evilbot")
        _write_zip(self.zip_path, [("../../evil.txt", "pwned")])

        list_fetch.process_fetched_list_zip("evilbot", self.zip_path)

        self.assertFalse(os.path.exists(extract_dir))

    # -------------------------------------------------------------- Zip bombs

    def test_a_declared_total_size_over_the_cap_is_rejected_before_extracting(self):
        self.set_config(MAX_FETCH_FILE_SIZE=100)
        big_txt = _list_txt() + ("!OtherBot Filler.flac  ::INFO:: 1.0MB\n" * 50)
        self.assertGreater(len(big_txt.encode("utf-8")), 100)
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", big_txt)])

        ok, reason = list_fetch.process_fetched_list_zip("bigbot", self.zip_path)

        self.assertFalse(ok)
        self.assertIn("exceeds", reason)
        self.assertNotIn("bigbot", config.fetched_bot_lists)
        self.assertFalse(os.path.exists(list_fetch.list_extract_dir("bigbot")))

    def test_an_oversized_zip_on_disk_is_rejected_before_it_is_opened(self):
        """#162 finding #10, belt-to-braces half: MAX_LIST_ZIP_ENTRIES and the
        declared-size guard both run on zf.infolist(), which zipfile.ZipFile()
        has ALREADY eagerly parsed (one ZipInfo per entry, before anything can
        refuse it) by the time either can act - the cost they exist to
        prevent is paid before they can prevent it. dcc_fetch.py's own
        MAX_FETCH_LIST_FILE_SIZE admission cap is what actually stops an
        oversized zip from ever reaching disk; this checks the belt this
        module keeps for itself regardless - a stat() on the file already
        sitting on disk, before opening it at all."""
        self.set_config(MAX_FETCH_LIST_FILE_SIZE=100)
        big_txt = _list_txt() + ("!OtherBot Filler.flac  ::INFO:: 1.0MB\n" * 50)
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", big_txt)])
        self.assertGreater(os.path.getsize(self.zip_path), 100)

        real_zipfile_open = __import__("zipfile").ZipFile

        def fail_if_opened(*a, **kw):
            self.fail("an oversized zip must be rejected before ZipFile() ever opens it")

        import zipfile as zipfile_module
        zipfile_module.ZipFile = fail_if_opened
        self.addCleanup(setattr, zipfile_module, "ZipFile", real_zipfile_open)

        ok, reason = list_fetch.process_fetched_list_zip("bigbot", self.zip_path)

        self.assertFalse(ok)
        self.assertIn("MAX_FETCH_LIST_FILE_SIZE", reason)
        self.assertNotIn("bigbot", config.fetched_bot_lists)
        self.assertFalse(os.path.exists(list_fetch.list_extract_dir("bigbot")))

    def test_a_zip_within_the_size_cap_is_not_rejected_by_it(self):
        """Control: a genuinely small list zip must not be caught by the new
        pre-open check just because it exists."""
        self.set_config(MAX_FETCH_LIST_FILE_SIZE=1_000_000)
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt())])

        ok, reason = list_fetch.process_fetched_list_zip("smallbot", self.zip_path)

        self.assertTrue(ok, reason)

    def test_too_many_entries_is_rejected(self):
        members = [(f"file{i}.txt", "x") for i in range(list_fetch.MAX_LIST_ZIP_ENTRIES + 1)]
        _write_zip(self.zip_path, members)

        ok, reason = list_fetch.process_fetched_list_zip("floodbot", self.zip_path)

        self.assertFalse(ok)
        self.assertIn("entries", reason)
        self.assertNotIn("floodbot", config.fetched_bot_lists)

    # ------------------------------------------------------ No recognizable list

    def test_a_zip_with_no_txt_file_at_all_fails_gracefully(self):
        _write_zip(self.zip_path, [("cover.jpg", b"\xff\xd8\xff\xe0notarealjpeg")])

        ok, reason = list_fetch.process_fetched_list_zip("nolistbot", self.zip_path)

        self.assertFalse(ok)
        self.assertIn("no recognizable", reason.lower())
        self.assertNotIn("nolistbot", config.fetched_bot_lists)

    def test_an_empty_zip_fails_gracefully(self):
        _write_zip(self.zip_path, [])

        ok, reason = list_fetch.process_fetched_list_zip("emptybot", self.zip_path)

        self.assertFalse(ok)
        self.assertIsNotNone(reason)

    def test_not_a_zip_file_at_all_fails_gracefully_not_a_crash(self):
        with open(self.zip_path, "wb") as f:
            f.write(b"this is definitely not a zip file")

        ok, reason = list_fetch.process_fetched_list_zip("badzipbot", self.zip_path)

        self.assertFalse(ok)
        self.assertIsNotNone(reason)

    def test_ambiguous_multiple_txt_files_picks_the_largest_without_crashing(self):
        small = "just a readme, not a list\n"
        big = _list_txt(files=(("Real Track.flac", "5.0MB"),)) * 5  # make it clearly bigger
        _write_zip(self.zip_path, [
            ("README.txt", small),
            ("OtherBot-2026-08-27.txt", big),
        ])

        ok, reason = list_fetch.process_fetched_list_zip("ambiguousbot", self.zip_path)

        self.assertTrue(ok, reason)
        titles = [row["title"] for row in _read_all(config.fetched_bot_lists["ambiguousbot"])]
        self.assertIn("Real Track.flac", titles)

    def test_the_rar_side_list_is_excluded_when_a_real_master_list_is_also_present(self):
        rar_txt = "List of Entire Album Folders (!rar)\n" + "=" * 90 + "\n"
        real_txt = _list_txt(files=(("Genuine.flac", "3.0MB"),))
        _write_zip(self.zip_path, [
            ("OtherBot-RAR-2026-08-27.txt", rar_txt),
            ("OtherBot-2026-08-27.txt", real_txt),
        ])

        ok, reason = list_fetch.process_fetched_list_zip("rarbot", self.zip_path)

        self.assertTrue(ok, reason)
        titles = [row["title"] for row in _read_all(config.fetched_bot_lists["rarbot"])]
        self.assertEqual(titles, ["Genuine.flac"])


class OnDemandReadingTests(DCCoreTestCase):
    """Issue #76, option 2: process_fetched_list_zip() no longer stores the
    parsed rows - only a path and a precomputed count - and
    get_fetched_bot_page() re-parses that path fresh on every call, the same
    "no caching between calls" contract webserver.build_filelists_payload()
    already has for this bot's own list.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-listfetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.zip_path = os.path.join(self.tmp, "incoming.zip")

    def test_entry_count_matches_the_real_post_dedup_row_count(self):
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt(files=(
            ("A.flac", "1.0MB"), ("B.flac", "2.0MB"),
            # Same filename+size twice - entries_to_filelist_rows() dedupes
            # this down to one row, and entry_count must reflect THAT count,
            # not the raw pre-dedup line count.
            ("A.flac", "1.0MB"),
        )))])

        ok, reason = list_fetch.process_fetched_list_zip("dedupbot", self.zip_path)
        self.assertTrue(ok, reason)

        entry = config.fetched_bot_lists["dedupbot"]
        rows = _read_all(entry)
        self.assertEqual(len(rows), 2)
        self.assertEqual(entry["entry_count"], len(rows))

    def test_on_demand_read_returns_the_same_rows_the_old_stored_approach_would_have(self):
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt(files=(
            ("Track One.flac", "10.0MB"),
        )))])
        ok, reason = list_fetch.process_fetched_list_zip("otherbot", self.zip_path)
        self.assertTrue(ok, reason)
        entry = config.fetched_bot_lists["otherbot"]

        # What the OLD code would have stored under "entries", reconstructed
        # independently via the same list.py pipeline this test module
        # already trusts (see _list_txt()/write_master_list() elsewhere).
        import list as list_mod
        expected_entries, _total = list_mod.find_matching_entries(
            [], limit=None, list_path=entry["list_path"])
        expected_rows = list_mod.entries_to_filelist_rows(expected_entries, "otherbot")

        rows = _read_all(entry)
        self.assertEqual(rows, expected_rows)

    def test_the_extracted_file_survives_on_disk_after_a_successful_fetch(self):
        """Nothing deletes the extracted .txt after a successful fetch - it
        must still be there for a LATER, independent on-demand read to work,
        including one long after process_fetched_list_zip() itself returned."""
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt())])
        list_fetch.process_fetched_list_zip("otherbot", self.zip_path)

        entry = config.fetched_bot_lists["otherbot"]
        self.assertTrue(os.path.exists(entry["list_path"]))
        # A second, independent read - proving no state from the first read
        # was needed for this to work.
        rows = _read_all(entry)
        self.assertEqual([r["title"] for r in rows], ["Track One.flac"])

    def test_a_missing_list_path_is_handled_gracefully_not_raised(self):
        """The operator manually clears data/fetched/ (or some other bug
        removes the file) after a successful fetch - a later on-demand read
        must return a clear error, never an unhandled exception."""
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt())])
        list_fetch.process_fetched_list_zip("otherbot", self.zip_path)
        entry = config.fetched_bot_lists["otherbot"]

        os.remove(entry["list_path"])

        rows, folders, files, error = list_fetch.get_fetched_bot_page(entry, 0, 100)
        self.assertEqual(rows, [])
        self.assertEqual((folders, files), (0, 0))
        self.assertIsNotNone(error)
        self.assertIn("otherbot", error.lower())

    def test_an_unreadable_list_path_is_handled_gracefully_not_raised(self):
        """A file that still exists but can no longer be opened (permissions
        changed, a network mount dropped) is the same class of problem as a
        missing file, just discovered a moment later - inside the open()
        call rather than an os.path.exists() check."""
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt())])
        list_fetch.process_fetched_list_zip("otherbot", self.zip_path)
        entry = config.fetched_bot_lists["otherbot"]

        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("permission bits are not enforced for this process "
                           "(root, or not POSIX) - cannot exercise this path")
        os.chmod(entry["list_path"], 0o000)
        self.addCleanup(os.chmod, entry["list_path"], 0o644)

        rows, folders, files, error = list_fetch.get_fetched_bot_page(entry, 0, 100)
        self.assertEqual(rows, [])
        self.assertEqual((folders, files), (0, 0))
        self.assertIsNotNone(error)

    def test_a_missing_list_path_field_is_handled_gracefully(self):
        """Defense in depth: an entry somehow missing the field entirely
        (a future bug, or hand-edited state) must not raise either."""
        rows, folders, files, error = list_fetch.get_fetched_bot_page(
            {"bot": "ghostbot"}, 0, 100)
        self.assertEqual(rows, [])
        self.assertEqual((folders, files), (0, 0))
        self.assertIsNotNone(error)

    def test_pagination_slices_the_freshly_parsed_rows(self):
        # Forward slashes: the folder is only a grouping key here, and
        # backslashes in a test literal buy nothing but escaping bugs.
        folders = [(f"D:/MUSIC/Album {i:02d}/",
                    [(f"Track {i:02d}.flac", "1.0MB")]) for i in range(10)]
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt",
                                    _list_txt_folders("OtherBot", folders))])
        list_fetch.process_fetched_list_zip("pagebot", self.zip_path)
        entry = config.fetched_bot_lists["pagebot"]

        page, total_folders, total_files, error = list_fetch.get_fetched_bot_page(
            entry, 3, 4)
        self.assertIsNone(error)
        self.assertEqual(total_folders, 10, "offset/limit count folders")
        self.assertEqual(total_files, 10)
        self.assertEqual([g["folder"].rstrip("/") for g in page],
                         ["D:/MUSIC/Album 03", "D:/MUSIC/Album 04",
                          "D:/MUSIC/Album 05", "D:/MUSIC/Album 06"])
        # Whole folders, never a partial one.
        for group in page:
            self.assertEqual(len(group["entries"]), group["count"])

    def test_an_offset_past_the_end_returns_an_empty_page_with_the_correct_total(self):
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt())])
        list_fetch.process_fetched_list_zip("otherbot", self.zip_path)
        entry = config.fetched_bot_lists["otherbot"]

        page, total_folders, total_files, error = list_fetch.get_fetched_bot_page(
            entry, 999, 100)
        self.assertIsNone(error)
        self.assertEqual(page, [])
        self.assertEqual(total_folders, 1, "one folder in this fixture")
        self.assertEqual(total_files, 1)


class SurvivingARestart(DCCoreTestCase):
    """The extracted files under FETCHED_FILES_DIR were always untouched by a
    restart - only config.fetched_bot_lists, the daemon's in-memory map of
    which bots they belong to, was not. Reported live: an operator had real
    fetched lists sitting on disk, restarted the bot, and the File Lists
    switcher had no memory of any of them."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-listfetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.zip_path = os.path.join(self.tmp, "incoming.zip")

        self.registry_path = os.path.join(self.tmp, "fetched_bot_lists.json")
        self.set_config(FETCHED_BOT_LISTS_FILE=self.registry_path)
        previous = db.FETCHED_BOT_LISTS_FILE
        db.FETCHED_BOT_LISTS_FILE = self.registry_path
        self.addCleanup(setattr, db, "FETCHED_BOT_LISTS_FILE", previous)

    def test_a_fetch_survives_a_restart(self):
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", _list_txt())])
        ok, reason = list_fetch.process_fetched_list_zip("otherbot", self.zip_path)
        self.assertTrue(ok, reason)

        # The "restart": memory forgotten, nothing but the disk files left.
        config.fetched_bot_lists.clear()

        config.fetched_bot_lists.update(db.load_fetched_bot_lists())

        self.assertIn("otherbot", config.fetched_bot_lists)
        entry = config.fetched_bot_lists["otherbot"]
        self.assertTrue(os.path.exists(entry["list_path"]),
                        "the registry survived, but points at a file that is gone")
        rows = _read_all(entry)
        self.assertEqual(len(rows), entry["entry_count"])

    def test_no_file_yet_is_an_empty_registry_not_an_error(self):
        self.assertEqual(db.load_fetched_bot_lists(), {})

    def test_an_unreadable_registry_does_not_stop_the_daemon(self):
        """The extracted files are untouched either way - a corrupt registry
        costs an empty switcher until the next fetch, not a refusal to boot."""
        with open(self.registry_path, "w", encoding="utf-8") as handle:
            handle.write("{not json at all")

        self.assertEqual(db.load_fetched_bot_lists(), {})

    def test_the_registry_is_loaded_at_startup(self):
        """The shape of #119: a correct, tested load/save pair that nothing
        on the boot path calls is exactly as unhelpful as no persistence at
        all - a unit test cannot see that oserve.py never asks for it."""
        with open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            lines = [line.strip() for line in handle.read().splitlines()
                     if not line.strip().startswith("#")]
        loads = [line for line in lines if "load_fetched_bot_lists(" in line]

        self.assertTrue(
            loads,
            "oserve.py does not load the fetched-lists registry at startup, "
            "so persisting it buys nothing - the File Lists switcher is "
            "empty on every restart regardless")


class ExtractedTextSizeCeiling(DCCoreTestCase):
    """Issue #76: nothing before this check bounds the number of LINES the
    extracted list contains - only the zip's own byte/member counts - and
    every line becomes a permanently-retained dict once parsed. Measured
    against production: a 20MB list arrived as a 0.8MB download, 143x smaller
    on the wire than what it expanded to in memory.

    This is checked on the extracted file's REAL size on disk, which is why
    a small, highly-compressible payload (repeated identical lines compress
    extremely well) is used below to prove this catches what the zip's own
    declared/compressed-size guard (MAX_FETCH_FILE_SIZE) cannot: the zip
    itself stays tiny, only the text after decompression is oversized.

    Deliberately NOT a subclass of SafeExtractionTests: this patches the
    module-level ceiling down to a size real fixture data in the tests there
    would exceed, so sharing that base would shrink the ceiling under them.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-listfetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.zip_path = os.path.join(self.tmp, "incoming.zip")

        self._original_limit = list_fetch.MAX_LIST_TEXT_SIZE
        self.addCleanup(setattr, list_fetch, "MAX_LIST_TEXT_SIZE", self._original_limit)
        list_fetch.MAX_LIST_TEXT_SIZE = 1000

    def test_an_extracted_list_over_the_ceiling_is_rejected(self):
        # Highly repetitive text compresses to a fraction of its real size -
        # the zip itself stays small even though the extracted text does not.
        oversized_txt = _list_txt() + ("!OtherBot Filler.flac  ::INFO:: 1.0MB\n" * 200)
        self.assertGreater(len(oversized_txt.encode("utf-8")), 1000)
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", oversized_txt)])
        # Prove the zip's OWN size guard would not have caught this alone -
        # this is specifically the gap issue #76 describes, not a duplicate
        # of the existing zip-bomb protection.
        self.assertLess(os.path.getsize(self.zip_path), 1000,
                        "fixture invariant: the zip itself must stay under the "
                        "text ceiling, or this isn't testing the gap in #76")

        ok, reason = list_fetch.process_fetched_list_zip("bigtextbot", self.zip_path)

        self.assertFalse(ok)
        self.assertIn("1000", reason)
        self.assertNotIn("bigtextbot", config.fetched_bot_lists)

    def test_the_extraction_directory_is_cleaned_up_on_rejection(self):
        oversized_txt = _list_txt() + ("!OtherBot Filler.flac  ::INFO:: 1.0MB\n" * 200)
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", oversized_txt)])

        list_fetch.process_fetched_list_zip("bigtextbot", self.zip_path)

        self.assertFalse(os.path.exists(list_fetch.list_extract_dir("bigtextbot")))

    def test_a_list_at_or_under_the_ceiling_still_works(self):
        """Control - the check must not start rejecting ordinary lists."""
        small_txt = _list_txt()
        self.assertLessEqual(len(small_txt.encode("utf-8")), 1000)
        _write_zip(self.zip_path, [("OtherBot-2026-08-27.txt", small_txt)])

        ok, reason = list_fetch.process_fetched_list_zip("smallbot", self.zip_path)

        self.assertTrue(ok, reason)
        self.assertIn("smallbot", config.fetched_bot_lists)

    def test_the_shipped_default_has_real_headroom_over_a_genuine_library(self):
        """Not a regression test for the patched value above - this pins the
        actual shipped default (20MB) against the number that motivated it:
        this operator's real 1.21TB/47,420-file library produces a 4MB list."""
        self.assertEqual(self._original_limit, 20 * 1024 * 1024)
        four_mb_real_library = 4 * 1024 * 1024
        self.assertGreater(self._original_limit, four_mb_real_library,
                           "the shipped ceiling no longer has headroom over a "
                           "real library-sized list")


class ListExtractDirTests(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-listfetch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

    def test_extract_dir_is_a_per_bot_subdirectory_under_lists(self):
        path = list_fetch.list_extract_dir("GoodBot")
        expected_root = os.path.join(os.path.abspath(self.tmp), "lists")
        self.assertTrue(path.startswith(expected_root + os.sep))
        self.assertIn("goodbot", path.lower())

    def test_path_separators_in_a_bot_nick_cannot_escape_the_lists_root(self):
        path = list_fetch.list_extract_dir("../../evil")
        expected_root = os.path.join(os.path.abspath(self.tmp), "lists")
        self.assertTrue(path == expected_root or path.startswith(expected_root + os.sep))

    def test_never_extracted_alongside_raw_fetched_files(self):
        """Distinct from FETCHED_FILES_DIR's top level - a fetched LIST
        archive must never be mistaken for a plain fetched file."""
        path = list_fetch.list_extract_dir("goodbot")
        self.assertNotEqual(os.path.dirname(path), os.path.abspath(self.tmp))




class AllDotsMemberNames(SafeExtractionTests):
    """A zip member whose path component is nothing but dots.

    ".." is caught by the containment check, because it genuinely resolves
    outside. Longer runs are not: "...." is a legal directory name that
    resolves INSIDE the target, so containment passes it - correctly.

    What breaks is what Win32 does with it afterwards. Trailing dots are
    stripped during path parsing, so "<extract>/...." resolves to "<extract>"
    itself. Extraction fails, the directory cannot then be cleaned, and every
    later fetch from that bot fails with:

        [WinError 145] The directory is not empty: ...\\lists\\<bot>\\....

    One hostile archive permanently disabled list fetching from that bot.
    """

    def _process(self, name, members):
        path = os.path.join(self.tmp, name)
        _write_zip(path, members)
        return list_fetch.process_fetched_list_zip("dotbot", path)

    def test_a_four_dot_component_is_refused(self):
        ok, reason = self._process("dots.zip", [("....//....//x.txt", "junk")])
        self.assertFalse(ok)
        self.assertIn("made only of dots", reason)

    def test_shorter_dot_runs_are_refused_too_by_whichever_check_sees_them(self):
        """The two checks are layered, and which one fires depends on how
        Win32 resolves the name.

        A SINGLE "...", "...." or "....." component is a multi-level parent
        reference there - N dots walks up N-1 levels - so containment catches
        those and reports traversal. Only the doubled "..../...." form
        resolves back INSIDE the target, passes containment, and reaches the
        dots check. Both outcomes are a refusal; this pins that none of them
        slips through either way.
        """
        for arc in ("../x.txt", ".../x.txt", "..../x.txt", "...../x.txt"):
            with self.subTest(arc=arc):
                ok, reason = self._process("d.zip", [(arc, "junk")])
                self.assertFalse(ok, f"{arc} was accepted")
                self.assertTrue(
                    "traversal" in reason.lower() or "made only of dots" in reason,
                    f"{arc} refused for an unexpected reason: {reason}")

    def test_a_two_dot_component_is_still_refused(self):
        """It was already refused, by the containment check - it must stay
        refused now that the dots check runs first."""
        ok, reason = self._process("dots2.zip", [("../x.txt", "junk")])
        self.assertFalse(ok)

    def test_the_bot_is_not_poisoned_for_later_fetches(self):
        """The actual regression. Before the fix the first archive left a
        directory that could not be removed, so this second, entirely
        legitimate fetch failed too - permanently.
        """
        bad, _ = self._process("bad.zip", [("....//....//x.txt", "junk")])
        self.assertFalse(bad)

        ok, reason = self._process(
            "good.zip", [("OtherBot-2026-08-27.txt", _list_txt())])
        self.assertTrue(ok, f"a later legitimate fetch was refused: {reason}")
        self.assertIn("dotbot", config.fetched_bot_lists)

    def test_a_dot_component_alone_is_still_ignored(self):
        """"." is a no-op path component, not an attack - it must not start
        being rejected."""
        path = os.path.join(self.tmp, "dot.zip")
        _write_zip(path, [("./OtherBot-2026-08-27.txt", _list_txt())])
        ok, reason = list_fetch.process_fetched_list_zip("plainbot", path)
        self.assertTrue(ok, f"a leading './' was wrongly refused: {reason}")


class ConcurrentFetchesForOneBot(SafeExtractionTests):
    """Two list fetches for the SAME bot completing at once.

    list_extract_dir() keys on the bot nick alone, and extraction opens by
    rmtree-ing that directory, so before the lock was widened to cover the
    whole sequence the second fetch deleted the first one's files while the
    first was still working inside them. The fetch slot pool lets several
    transfers finish together, so this needed no unusual timing to hit - and
    both calls still returned success, so nothing reported it.

    The interleaving is pinned rather than raced for: the first call is held
    just after its extraction finishes, the second then runs a complete fetch,
    and only then is the first released to parse whatever is left on disk.
    Both archives are clean and well formed - any failure here is the shared
    directory, not the input.
    """

    def _tagged_zip(self, tag, count):
        body = "".join(
            f"!SomeBot {tag}Track{i}.flac  ::INFO:: 5.00MB\n" for i in range(count))
        path = os.path.join(self.tmp, f"{tag}.zip")
        _write_zip(path, [(f"SomeBot-{tag}.txt", body)])
        return path

    def _run_pinned_pair(self):
        import threading
        bot = "samebot"
        zip_a = self._tagged_zip("A", 30)
        zip_b = self._tagged_zip("B", 70)

        a_extracted = threading.Event()
        b_finished = threading.Event()
        real_pick = list_fetch._pick_list_file
        guard = threading.Lock()
        seen = []

        def hooked(extract_dir):
            with guard:
                is_first = not seen
                seen.append(1)
            if is_first:
                a_extracted.set()
                # Once the lock covers the whole sequence the other thread is
                # blocked and cannot signal, so this times out instead. That
                # is the fix working, not the test failing - hence a short,
                # bounded wait rather than an indefinite one.
                b_finished.wait(timeout=1.0)
            return real_pick(extract_dir)

        list_fetch._pick_list_file = hooked
        self.addCleanup(setattr, list_fetch, "_pick_list_file", real_pick)

        results = {}

        def fetch_a():
            results["a"] = list_fetch.process_fetched_list_zip(bot, zip_a)

        def fetch_b():
            a_extracted.wait(timeout=5.0)
            results["b"] = list_fetch.process_fetched_list_zip(bot, zip_b)
            b_finished.set()

        threads = [threading.Thread(target=fetch_a, daemon=True),
                   threading.Thread(target=fetch_b, daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(),
                             "a fetch thread never finished - the widened lock "
                             "should serialise these two, not deadlock them")
        return bot, results

    def test_the_stored_record_matches_the_archive_it_names(self):
        """The defect itself.

        Unfixed, the first call parsed the SECOND call's extracted files and
        stored them under its own source_zip - a record labelled "A.zip"
        holding every row from B.zip, reported to the operator as a successful
        fetch of A.
        """
        bot, _results = self._run_pinned_pair()

        entry = config.fetched_bot_lists[bot]
        rows = _read_all(entry)
        archives = {row["title"][0] for row in rows if row.get("title")}
        self.assertEqual(
            len(archives), 1,
            f"the stored list mixes rows from both archives: {sorted(archives)}")
        self.assertEqual(
            entry["source_zip"][0], archives.pop(),
            f"the stored record names {entry['source_zip']} but its rows came "
            f"from the other archive - one fetch parsed the other's extracted "
            f"files after overwriting the shared extraction directory")

    def test_both_fetches_still_report_a_result(self):
        """Control. Serialising them must not make either call fail or hang -
        both archives are valid, so both fetches should succeed."""
        _bot, results = self._run_pinned_pair()

        for which in ("a", "b"):
            with self.subTest(fetch=which):
                self.assertIn(which, results, "the fetch never returned")
                ok, reason = results[which]
                self.assertTrue(ok, f"a clean archive was refused: {reason}")


class ConcurrentReadDuringSameBotRefetch(DCCoreTestCase):
    """Issue #76 regression: get_fetched_bot_page() used to take NO lock at
    all, while process_fetched_list_zip() extracts by rmtree-ing the shared
    extract_dir and then rewriting the exact same list_path in place
    (open(long_dest, "wb") - no temp-file-then-rename). A same-bot re-fetch
    reuses that exact path (list_extract_dir() keys only on the bot nick), so
    a concurrent read could land mid-rewrite: not "file missing" (already
    handled cleanly elsewhere as a clean error), but a torn, partially
    rewritten file silently parsed into a wrong, in-between row count - a
    plain 200-shaped result with no exception at all.

    This races a writer thread doing real re-fetches (no artificial pinning
    or hooks - the bug needs none) against reader threads calling
    get_fetched_bot_page() in a tight loop, and asserts every single read's
    `total` is one of the two genuinely valid row counts - never anything
    else. Before the fix (get_fetched_bot_page() sharing process_fetched_
    list_zip()'s _lock()), this reliably observed several dozen out-of-range
    totals; after it, zero, because a read can no longer land inside an
    in-progress rewrite.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-listfetch-race-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

    def _make_zip(self, path, count):
        # The SAME member name every time, so _pick_list_file() always
        # resolves to the identical extract_dir/SomeBot-List.txt path - the
        # exact condition the bug report describes: a same-bot re-fetch
        # reuses the EXACT SAME list_path an in-flight reader might already
        # be reading, rather than switching to a differently-named file
        # (which would just hit the already-handled "missing" path instead).
        body = "".join(
            f"!SomeBot Track{i}.flac  ::INFO:: 5.00MB\n" for i in range(count))
        _write_zip(path, [("SomeBot-List.txt", body)])

    def test_reads_never_observe_a_torn_total_during_concurrent_refetches(self):
        import threading

        bot = "racebot"
        count_a, count_b = 800, 1400
        zip_a = os.path.join(self.tmp, "a.zip")
        zip_b = os.path.join(self.tmp, "b.zip")
        self._make_zip(zip_a, count_a)
        self._make_zip(zip_b, count_b)

        # Seed an initial fetch so readers always have something on record
        # from the moment they start.
        ok, reason = list_fetch.process_fetched_list_zip(bot, zip_a)
        self.assertTrue(ok, reason)

        READER_THREADS = 2
        READS_PER_THREAD = 40
        # A generous safety cap on how many times the writer will re-fetch
        # while waiting for the readers to finish their fixed quota of reads
        # - not the thing that stops the writer under normal conditions (the
        # readers finishing is), just a backstop so a stuck reader cannot
        # wedge this test into looping forever.
        MAX_FETCH_ROUNDS = 2000
        stop_writer = threading.Event()
        state_lock = threading.Lock()
        errors = []
        bad_totals = []
        read_count = [0]
        readers_done = [0]

        def writer():
            i = 0
            while not stop_writer.is_set() and i < MAX_FETCH_ROUNDS:
                zip_path = zip_a if i % 2 == 0 else zip_b
                ok, reason = list_fetch.process_fetched_list_zip(bot, zip_path)
                if not ok:
                    with state_lock:
                        errors.append(f"re-fetch {i} on {zip_path!r} failed: {reason}")
                    break
                i += 1

        def reader():
            for _ in range(READS_PER_THREAD):
                entry = config.fetched_bot_lists.get(bot)
                # total_files, not the folder count: both archives here have
                # the same single folder, so only the ROW total can show a
                # read that landed between the two - which is the torn read
                # this test exists to catch.
                _page, _folders, total_files, error = list_fetch.get_fetched_bot_page(
                    entry, 0, 10 ** 9)
                with state_lock:
                    read_count[0] += 1
                    if error is None and total_files not in (count_a, count_b):
                        bad_totals.append(total_files)
            with state_lock:
                readers_done[0] += 1
                if readers_done[0] == READER_THREADS:
                    stop_writer.set()

        threads = [threading.Thread(target=writer, daemon=True)]
        threads += [threading.Thread(target=reader, daemon=True)
                    for _ in range(READER_THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(),
                             "a writer/reader thread never finished within the "
                             "timeout - possible deadlock between the read and "
                             "write lock paths")

        self.assertEqual(errors, [], f"a re-fetch failed unexpectedly: {errors}")
        self.assertEqual(
            read_count[0], READER_THREADS * READS_PER_THREAD,
            "not every reader thread completed its full quota of reads")
        self.assertEqual(
            bad_totals, [],
            f"observed {len(bad_totals)} torn read(s) out of {read_count[0]} "
            f"whose total was neither {count_a} nor {count_b} (a same-bot "
            f"re-fetch raced a read into a partially-rewritten list file): "
            f"{bad_totals[:10]}")


class FallbackLockIsShared(DCCoreTestCase):
    """_lock() falls back to a module-level lock when oserve.py has not run.

    It used to build `threading.Lock()` inline, handing every caller a brand
    new object that each acquired uncontended - so the fallback path
    synchronised nothing at all, silently.
    """

    def test_repeated_calls_return_one_object(self):
        saved = getattr(config, "fetched_bot_lists_lock", None)
        if saved is not None:
            del config.fetched_bot_lists_lock
            self.addCleanup(setattr, config, "fetched_bot_lists_lock", saved)

        self.assertIs(list_fetch._lock(), list_fetch._lock(),
                      "the fallback hands out a fresh lock per call, so "
                      "concurrent callers never actually exclude each other")


def _crafted_zip(path, mutate):
    """A valid deflated archive whose bytes are then rewritten in place.

    Every case below has correct headers - the archive opens, infolist() is
    clean, the member count and sizes all pass the existing guards. Only
    reading the member out fails, which is why these got past everything.
    """
    body = ("!SomeBot Track.flac  ::INFO:: 5.00MB\n" * 300).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SomeBot-List.txt", body)
    with open(path, "rb") as handle:
        raw = bytearray(handle.read())
    with zipfile.ZipFile(path) as archive:
        local_offset = archive.infolist()[0].header_offset
    mutate(raw, local_offset, raw.find(b"PK\x01\x02"))
    with open(path, "wb") as handle:
        handle.write(bytes(raw))
    return path


def _corrupt_the_stream(raw, local_offset, _central_offset):
    """Garbage inside the compressed data -> zlib.error on decompression."""
    name_len, extra_len = struct.unpack("<HH", raw[local_offset + 26:local_offset + 30])
    start = local_offset + 30 + name_len + extra_len
    for index in range(start + 12, start + 30):
        raw[index] ^= 0xFF


def _unknown_compression_method(raw, local_offset, central_offset):
    """Method 99 -> NotImplementedError, no decompressor exists."""
    struct.pack_into("<H", raw, local_offset + 8, 99)
    struct.pack_into("<H", raw, central_offset + 10, 99)


def _flag_as_encrypted(raw, local_offset, central_offset):
    """General-purpose flag bit 0 -> RuntimeError, password required."""
    struct.pack_into("<H", raw, local_offset + 6, 0x0001)
    struct.pack_into("<H", raw, central_offset + 8, 0x0001)


class MalformedArchivesZipfileLeaks(SafeExtractionTests):
    """Archives whose headers parse cleanly but whose contents cannot be read.

    The guard caught (BadZipFile, ValueError, OSError). zipfile raises three
    more for a hand-crafted archive, and none of them subclass those:

      * zlib.error          - the deflate stream is corrupt
      * NotImplementedError - a compression method with no decompressor
      * RuntimeError        - a member flagged encrypted, no password given

    A remote peer chooses these bytes and process_fetched_list_zip() is
    documented "Never raises", so each one reached the fetch thread instead
    of being reported as a refused list.
    """

    def _refused(self, name, mutate):
        path = _crafted_zip(os.path.join(self.tmp, name), mutate)
        ok, reason = list_fetch.process_fetched_list_zip("probebot", path)
        self.assertFalse(ok, "a broken archive was accepted")
        self.assertIn("extraction aborted", reason)
        return reason

    def test_a_corrupt_deflate_stream_is_refused(self):
        reason = self._refused("corrupt.zip", _corrupt_the_stream)
        self.assertIn("decompressing", reason)

    def test_an_unknown_compression_method_is_refused(self):
        reason = self._refused("method.zip", _unknown_compression_method)
        self.assertIn("NotImplementedError", reason)

    def test_a_member_flagged_encrypted_is_refused(self):
        reason = self._refused("encrypted.zip", _flag_as_encrypted)
        self.assertIn("encrypted", reason)

    def test_the_extraction_directory_is_cleaned_up(self):
        """Every other abort path removes the half-extracted directory. These
        must too, or the next fetch from that bot inherits the debris - the
        same way the all-dots archive used to poison a bot permanently."""
        self._refused("corrupt2.zip", _corrupt_the_stream)
        self.assertFalse(
            os.path.exists(list_fetch.list_extract_dir("probebot")),
            "the aborted extraction left its directory behind")

    def test_the_cleanup_uses_long_path_like_every_other_one_in_this_file(self):
        """#222: platform_compat.long_path() is identity on Linux, so the
        test above passes whether or not this branch's rmtree() is wrapped -
        it cannot distinguish the bug from the fix on this platform. This
        branch matters more than the others it stands beside: it is the one
        a hand-crafted archive with long member paths actually reaches, so on
        Windows it is the one most likely to be cleaning up a path over the
        260-character limit, where ignore_errors=True would otherwise
        silently leave the debris under FETCHED_FILES_DIR forever."""
        import ast

        with io.open(list_fetch.__file__, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)

        bare = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "rmtree"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            wrapped = (isinstance(first, ast.Call) and isinstance(first.func, ast.Attribute)
                      and first.func.attr == "long_path")
            if not wrapped:
                bare.append(node.lineno)

        self.assertEqual(bare, [], f"rmtree() called without long_path() at line(s): {bare}")

    def test_a_sound_archive_is_still_accepted(self):
        """Control. The wider guard must not start swallowing good ones."""
        path = os.path.join(self.tmp, "fine.zip")
        _write_zip(path, [("OtherBot-2026-08-27.txt", _list_txt())])
        ok, reason = list_fetch.process_fetched_list_zip("goodbot", path)
        self.assertTrue(ok, f"a valid archive was refused: {reason}")


class LongMemberNames(SafeExtractionTests):
    """A zip member name is chosen by the remote bot and never truncated, so
    its length is entirely that bot's decision.

    dcc.py wraps every path it touches in platform_compat.long_path(); the
    extraction path added on this branch did not. A perfectly legal 240-
    character member name pushes the destination past Windows' 260-character
    MAX_PATH, and the whole archive was refused with:

        extraction aborted: [Errno 2] No such file or directory

    for a file this code is itself trying to create. The name is legal on
    Linux, so this only ever bit on Windows.
    """

    LONG_NAME = "SomeBot-" + ("L" * 230) + ".txt"      # 242 characters

    def test_a_long_member_name_extracts_parses_and_stores(self):
        path = os.path.join(self.tmp, "long.zip")
        _write_zip(path, [(self.LONG_NAME, _list_txt())])

        ok, reason = list_fetch.process_fetched_list_zip("longbot", path)

        self.assertTrue(ok, f"a legal long member name was refused: {reason}")
        entry = config.fetched_bot_lists["longbot"]
        self.assertEqual([row["title"] for row in _read_all(entry)],
                         ["Track One.flac"])

    def test_the_destination_really_is_past_max_path(self):
        """Fixture invariant. If the temp root is short enough that the
        destination lands under 260, the test above proves nothing on Windows
        and this says so instead of passing quietly."""
        dest = os.path.join(list_fetch.list_extract_dir("longbot"), self.LONG_NAME)
        self.assertGreater(
            len(dest), 260,
            f"the destination is only {len(dest)} characters, so the MAX_PATH "
            f"hazard is not being exercised")

    def test_the_hazard_is_real_on_this_machine(self):
        """Control. Windows 10 1607+ can switch MAX_PATH off entirely via the
        LongPathsEnabled registry value, and GitHub's windows-latest images
        ship with it ON - so on those runners the test above would pass even
        unfixed. Skip rather than assert something untrue of the machine."""
        from tests.test_long_paths import MAX_PATH_ENFORCED
        if not MAX_PATH_ENFORCED:
            self.skipTest("this machine does not enforce MAX_PATH")

        dest = os.path.join(list_fetch.list_extract_dir("probe"), self.LONG_NAME)
        os.makedirs(list_fetch.list_extract_dir("probe"), exist_ok=True)
        with self.assertRaises(OSError):
            open(dest, "wb").close()

    def test_a_long_named_leftover_can_still_be_cleaned_up(self):
        """The other half: extraction opens by rmtree-ing the directory, and
        rmtree cannot delete what it cannot open. A long-named file left by
        one archive would otherwise block every later fetch from that bot -
        the same way the all-dots archive did in #75."""
        first = os.path.join(self.tmp, "first.zip")
        _write_zip(first, [(self.LONG_NAME, _list_txt())])
        self.assertTrue(list_fetch.process_fetched_list_zip("longbot", first)[0])

        second = os.path.join(self.tmp, "second.zip")
        _write_zip(second, [("OtherBot-2026-08-27.txt", _list_txt())])
        ok, reason = list_fetch.process_fetched_list_zip("longbot", second)
        self.assertTrue(ok, f"a later fetch was blocked by the leftover: {reason}")




class FolderGrouping(unittest.TestCase):
    """group_rows_by_folder(): the shape the File Lists view renders."""

    def rows(self, *pairs):
        return [{"folder": folder, "title": title, "size": "1.0MB",
                 "format": "FLAC", "source": "bot"} for folder, title in pairs]

    def test_rows_group_under_their_folder_in_first_seen_order(self):
        """First-seen, not sorted: the master list's own order is the order the
        operator recognises from their own disk."""
        groups = list_module.group_rows_by_folder(self.rows(
            ("Z/", "one"), ("A/", "two"), ("Z/", "three")))

        self.assertEqual([g["folder"] for g in groups], ["Z/", "A/"])

    def test_a_folder_that_reappears_later_rejoins_its_first_group(self):
        """Rows for one folder are not required to be adjacent - a list can
        interleave them, and each folder must still appear once."""
        groups = list_module.group_rows_by_folder(self.rows(
            ("A/", "one"), ("B/", "two"), ("A/", "three")))

        self.assertEqual(len(groups), 2)
        self.assertEqual([r["title"] for r in groups[0]["entries"]], ["one", "three"])

    def test_the_count_matches_the_entries(self):
        groups = list_module.group_rows_by_folder(self.rows(
            ("A/", "one"), ("A/", "two"), ("B/", "three")))

        for group in groups:
            self.assertEqual(group["count"], len(group["entries"]), group["folder"])

    def test_rows_with_no_folder_are_kept_under_one_empty_name(self):
        """A foreign bot's list may carry no folder headings at all. Those rows
        are grouped, not dropped - the view labels the empty name itself."""
        groups = list_module.group_rows_by_folder(
            [{"folder": "", "title": "one"}, {"folder": None, "title": "two"}])

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["folder"], "")
        self.assertEqual(groups[0]["count"], 2)


class FolderPaging(unittest.TestCase):
    """page_folder_groups(): the page is a whole number of folders."""

    def groups(self, sizes):
        return [{"folder": "F%02d" % i, "count": size,
                 "entries": [{"title": "t%d" % j} for j in range(size)]}
                for i, size in enumerate(sizes)]

    def test_a_page_holds_whole_folders_and_reports_both_totals(self):
        page, folders, rows = list_module.page_folder_groups(
            self.groups([3, 4, 5]), 0, 2)

        self.assertEqual([g["folder"] for g in page], ["F00", "F01"])
        self.assertEqual(folders, 3, "three folders in the list")
        self.assertEqual(rows, 12, "twelve files across all three, not just this page")

    def test_offset_past_the_end_is_empty_with_the_totals_intact(self):
        page, folders, rows = list_module.page_folder_groups(
            self.groups([3, 4]), 99, 10)

        self.assertEqual(page, [])
        self.assertEqual((folders, rows), (2, 7))

    def test_a_negative_offset_reads_from_the_start(self):
        page, _folders, _rows = list_module.page_folder_groups(
            self.groups([3, 4]), -5, 1)

        self.assertEqual([g["folder"] for g in page], ["F00"])

    def test_the_row_ceiling_ends_a_page_before_the_folder_limit(self):
        """Folder sizes are uneven, so a folder count alone does not bound the
        response - which is the unbounded payload issue #76 removed."""
        page, _folders, _rows = list_module.page_folder_groups(
            self.groups([40, 40, 40]), 0, 10, max_rows=100)

        self.assertEqual(len(page), 2, "the third would take it to 120 rows")
        self.assertEqual(sum(g["count"] for g in page), 80)

    def test_one_folder_larger_than_the_ceiling_is_cut_but_still_returned(self):
        """The one place a folder is split. Returning nothing would leave the
        caller unable to advance past it, so it comes back cut and flagged -
        and `count` keeps reporting the true size."""
        page, _folders, _rows = list_module.page_folder_groups(
            self.groups([500]), 0, 10, max_rows=100)

        self.assertEqual(len(page), 1)
        self.assertEqual(len(page[0]["entries"]), 100, "cut to the ceiling")
        self.assertEqual(page[0]["count"], 500, "still reports what it holds")
        self.assertTrue(page[0]["truncated"])

    def test_an_untruncated_folder_carries_no_truncated_flag(self):
        """Control: the view only marks a folder cut short, so the flag must
        be absent - or falsey - everywhere else."""
        page, _folders, _rows = list_module.page_folder_groups(
            self.groups([3]), 0, 10, max_rows=100)

        self.assertFalse(page[0].get("truncated"))

    def test_walking_by_the_number_returned_visits_every_folder_once(self):
        """THE CONTRACT THE PAGER DEPENDS ON.

        A page is bounded by TWO limits - the folder count asked for, and the
        row ceiling - so a request for 200 folders can come back with 41. A
        caller that advances by the number it ASKED for therefore steps clean
        over the folders it was not given, and they become unreachable: no
        error, no gap in the display, just files that cannot be browsed to.

        Advancing by the number RETURNED is what makes the walk complete, so
        this pins it against a library the ceiling actually bites on.
        """
        groups = self.groups([60] * 200)
        limit, ceiling = 200, 500

        offset, seen, guard = 0, [], 0
        while True:
            guard += 1
            self.assertLess(guard, 1000, "the walk failed to terminate")
            page, total, _rows = list_module.page_folder_groups(
                groups, offset, limit, max_rows=ceiling)
            seen.extend(g["folder"] for g in page)
            if not page or offset + len(page) >= total:
                break
            offset += len(page)

        self.assertLess(guard, 200, "the ceiling should not reduce pages to one folder")
        self.assertEqual(len(seen), 200, "every folder was reached")
        self.assertEqual(len(set(seen)), 200, "and none was served twice")


if __name__ == "__main__":
    unittest.main()
