"""The list publishes first, and audio lengths are read afterwards (#1182).

With LIST_SHOW_AUDIO_INFO on, a rebuild read every new or changed audio file
BEFORE it wrote the list, capped by LIST_AUDIO_INFO_MINUTES; whatever was not
read in time waited for the next rebuild, and nobody could tell how far the
reading had got. Now the rebuild publishes first with what the cache already
knows, the same process reads the rest with no time limit, saving to the cache
as it goes, and then rewrites only the length and quality into the published
list - no new scan, the same swap, the archive rebuilt, and the list's date
kept so other bots do not fetch it twice.

What is pinned here:
- the order: the list is swapped in before any file is read;
- the rewrite is byte-identical to a full rebuild with the same cache;
- nothing is published again when no row changed;
- a stopped reading keeps what it read, and saves as it goes;
- the reading-only run finds what the published list has no length for;
- the phases and the result line.

File names are invented; the audio is built byte by byte by the #567 tests'
helpers.
"""

import io
import json
import os
import sqlite3
import sys
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import audio_info  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
# The module, not its classes: a TestCase imported by name runs again here.
import tests.test_the_list_says_how_long_and_how_good as long_and_good  # noqa: E402

frames = long_and_good.frames
flac = long_and_good.flac


class Library(DCCoreTestCase):
    """A small library with readable, unreadable and non-audio files."""

    fmt = "txt"

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree(tracks=())
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, LIST_BASE_NAME="SomeBot",
                        NICKNAME="SomeBot", ORIGINAL_NICK="SomeBot", RAR_ENABLED=True,
                        LIST_FORMAT=self.fmt, LIST_SHOW_AUDIO_INFO=True,
                        LIST_AUDIO_INFO_THREADS=2)
        self.write("Album One", "Example Artist - 01 - Opening.mp3", frames(1000))
        self.write("Album One", "Example Artist - 02 - Closing.flac", flac())
        self.write("Album One", "Cover.jpg", b"\xff\xd8" + b"\x00" * 300)
        self.write("Album One", "Broken.mp3", b"not audio at all")
        self.write("Album Two", "Example Artist - 01 - Second.mp3", frames(500, mode=0x00))
        self.write("Films", "Some.Film.2021.mkv", b"\x1a\x45\xdf\xa3" + b"\x00" * 200)
        # As the bot starts it (#1182 audit): a run started by hand does not
        # read in the background.
        patcher = mock.patch.dict(os.environ, {update_list.RUN_TOKEN_ENV: "a-run-of-the-bot"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, folder, name, data):
        directory = os.path.join(self.tree.music, folder)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "wb") as handle:
            handle.write(data)

    def rebuild(self, read=True, stop=None):
        """One rebuild as __main__ runs it: publish, then the reading."""
        jobs = []
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertTrue(update_list.generate_master_list(reading_jobs=jobs))
            result = None
            if read and jobs:
                result = (update_list.run_audio_reading(jobs) if stop is None
                          else update_list.read_audio_info(jobs, stop=stop))
        return jobs, result, out.getvalue()

    def master(self):
        return list_mod.find_latest_list()

    def master_bytes(self):
        with open(self.master(), "rb") as handle:
            return handle.read()

    def artifact(self):
        return list_mod.find_latest_list_file()

    def tails(self):
        with open(self.master(), encoding="utf-8") as handle:
            return {line.split("  ::INFO:: ")[0].split(" ", 1)[1]: line.rstrip("\n").split("  ::INFO:: ")[1]
                    for line in handle if line.startswith("!")}

    def cache_keys(self):
        conn = sqlite3.connect(audio_info.cache_path())
        try:
            return {row[0] for row in conn.execute("SELECT key FROM audio")}
        finally:
            conn.close()


class PublishFirst(Library):

    def test_the_list_is_swapped_in_before_any_file_is_read(self):
        events = []
        real_publish = update_list._publish_artifacts
        real_read = audio_info.read

        def publish(swaps):
            events.append("swap")
            return real_publish(swaps)

        def read(path, size=None):
            events.append("read")
            return real_read(path, size)

        with mock.patch.object(update_list, "_publish_artifacts", publish), \
                mock.patch.object(audio_info, "read", read):
            jobs, _result, _said = self.rebuild(read=False)
            self.assertEqual(events, ["swap"], "the rebuild read nothing before publishing")
            self.assertTrue(all(" " not in tail for tail in self.tails().values()),
                            "published with sizes alone: nothing was in the cache")
            with redirect_stdout(io.StringIO()):
                update_list.run_audio_reading(jobs)
        self.assertEqual(events[0], "swap")
        self.assertEqual(events.count("read"), 4, "the four audio files, after the swap")
        self.assertEqual(events[-1], "swap", "and then once more, with their lengths")

    def test_the_rewrite_keeps_the_lists_date(self):
        self.rebuild(read=False)
        stamp = os.stat(self.master()).st_mtime_ns
        artifact_stamp = os.stat(self.artifact()).st_mtime_ns
        time.sleep(0.05)
        out = io.StringIO()
        with redirect_stdout(out):
            update_list.run_audio_reading()
        self.assertIn("list updated", out.getvalue())
        self.assertEqual(os.stat(self.master()).st_mtime_ns, stamp)
        self.assertEqual(os.stat(self.artifact()).st_mtime_ns, artifact_stamp)
        self.assertRegex(self.tails()["Example Artist - 01 - Opening.mp3"], r" 0m26s 128/44\.1/JS$")


class TheRewriteIsAFullRebuild(Library):
    """The suffix-only rewrite is byte-identical to a full rebuild with the
    same cache. The clock is held still so the two headers - which name how
    long the scan took - say the same."""

    def frozen(self):
        return mock.patch("time.time", return_value=1_790_000_000.0)

    def members(self, path):
        if path.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                return {name: archive.read(name) for name in archive.namelist()}
        with open(path, "rb") as handle:
            return {os.path.basename(path): handle.read()}

    def test_byte_identical(self):
        with self.frozen():
            self.rebuild()
            rewritten, archive = self.master_bytes(), self.members(self.artifact())
            self.assertIn(b" 0m26s 128/44.1/JS", rewritten)
            jobs, _result, _said = self.rebuild()
        self.assertEqual(jobs, [], "the cache knew every file: nothing left to read")
        self.assertEqual(self.master_bytes(), rewritten)
        self.assertEqual(self.members(self.artifact()), archive)


class TheRewriteIsAFullRebuildZipped(TheRewriteIsAFullRebuild):
    fmt = "zip"

    def test_the_archive_holds_the_album_and_film_lists_too(self):
        with self.frozen():
            self.rebuild()
        names = sorted(self.members(self.artifact()))
        self.assertEqual(len(names), 3, names)
        self.assertTrue(any("-RAR-" in name for name in names), names)
        self.assertTrue(any("-VIDEO-" in name for name in names), names)


class NothingChangedNothingPublished(Library):

    def test_a_reading_that_finds_no_length_publishes_nothing(self):
        """Only unreadable files were new: no row's suffix changed."""
        self.rebuild()
        self.write("Album Three", "Also Broken.mp3", b"still not audio")
        swaps = []
        real_publish = update_list._publish_artifacts
        with mock.patch.object(update_list, "_publish_artifacts",
                               lambda pairs: swaps.append(pairs) or real_publish(pairs)):
            _jobs, result, said = self.rebuild()
        self.assertEqual(len(swaps), 1, "the rebuild's own swap and no second one")
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["updated"], [])
        self.assertIn("Audio info: done: 0 read, 1 unreadable, the list did not change.", said)

    def test_the_rewrite_itself_refuses_when_no_row_changed(self):
        self.rebuild()
        before = os.stat(self.master())
        self.assertEqual(update_list.rewrite_audio_info(), update_list.REWRITE_UNCHANGED)
        after = os.stat(self.master())
        self.assertEqual((after.st_mtime_ns, after.st_size, after.st_ino),
                         (before.st_mtime_ns, before.st_size, before.st_ino))
        self.assertFalse(os.path.exists(self.master() + ".new"), "its temporary is gone")

    def test_a_list_published_meanwhile_is_not_overwritten(self):
        """A rebuild that published while the reading ran wins."""
        self.rebuild(read=False)
        real_build = update_list.build_list_artifact

        def build_then_touch(*args, **kwargs):
            built = real_build(*args, **kwargs)
            with open(self.master(), "a", encoding="utf-8") as handle:
                handle.write("\n")
            return built

        with mock.patch.object(update_list, "build_list_artifact", build_then_touch):
            with redirect_stdout(io.StringIO()):
                result = update_list.run_audio_reading()
        self.assertEqual(result["updated"], [])
        self.assertTrue(all(" " not in tail for tail in self.tails().values()))
        leftovers = [name for name in os.listdir(self.tree.lists) if name.endswith(".new")]
        self.assertEqual(leftovers, [])


class AStoppedReadingKeepsWhatItRead(Library):

    def test_stopped_part_way(self):
        answers = iter([False, False] + [True] * 500)
        _jobs, result, _said = self.rebuild(stop=lambda: next(answers))
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(result["read"], 2)
        self.assertEqual(len(self.cache_keys()), 2, "both reads were kept")
        self.assertTrue(all(" " not in tail for tail in self.tails().values()),
                        "a stopped reading does not publish")
        self.assertIn("stopped: 2 of 4 read and kept",
                      update_list.describe_audio_result(result))

    def test_it_saves_as_it_goes(self):
        """The database holds the first reads while later ones are still
        running - not only at the end, which a killed reading never reaches."""
        jobs, _result, _said = self.rebuild(read=False)
        stored_when_reading = []
        real_read = audio_info.read

        def read(path, size=None):
            stored_when_reading.append(len(self.cache_keys()))
            return real_read(path, size)

        with mock.patch.object(update_list, "AUDIO_SAVE_EVERY", 1), \
                mock.patch.object(audio_info, "read", read), \
                redirect_stdout(io.StringIO()):
            self.set_config(LIST_AUDIO_INFO_THREADS=1)
            update_list.run_audio_reading(jobs)
        self.assertGreater(max(stored_when_reading), 0, stored_when_reading)

    def test_the_stop_request_file(self):
        self.assertFalse(update_list.stop_requested())
        self.assertTrue(update_list.request_stop())
        self.assertTrue(update_list.stop_requested())
        jobs, _result, _said = self.rebuild(read=False)
        with redirect_stdout(io.StringIO()):
            result = update_list.run_audio_reading(jobs)
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(result["read"], 0, "asked to stop before the first read")
        self.assertFalse(update_list.stop_requested(), "a reading that obeyed it clears it")


class TheReadingOnlyRun(Library):

    def test_it_finds_what_the_published_list_has_no_length_for(self):
        self.rebuild(read=False)
        out = io.StringIO()
        with redirect_stdout(out):
            result = update_list.run_audio_reading()
        self.assertEqual((result["outcome"], result["total"], result["read"]), ("done", 4, 4))
        self.assertEqual(result["updated"], ["Main"])
        self.assertRegex(self.tails()["Example Artist - 01 - Second.mp3"], r" 0m13s 128/44\.1/S$")
        self.assertRegex(self.tails()["Example Artist - 02 - Closing.flac"], r" 0m2s 1115/44\.1/S$")
        with redirect_stdout(io.StringIO()) as again:
            result = update_list.run_audio_reading()
        self.assertEqual(result["outcome"], "nothing")
        self.assertIn("[AUDIO-INFO] Audio info: nothing new to read.", again.getvalue())

    def test_its_keys_are_the_rebuilds(self):
        """Read from the list, each file lands under the key the next walk
        looks it up by - or the next rebuild would read everything again."""
        self.rebuild(read=False)
        with redirect_stdout(io.StringIO()):
            update_list.run_audio_reading()
        jobs, _result, said = self.rebuild(read=False)
        self.assertEqual(jobs, [], said)

    def test_off_reads_nothing(self):
        self.rebuild(read=False)
        self.set_config(LIST_SHOW_AUDIO_INFO=False)
        with mock.patch.object(audio_info, "read", side_effect=AssertionError("read")), \
                redirect_stdout(io.StringIO()) as out:
            result = update_list.run_audio_reading()
        self.assertEqual(result["outcome"], "off")
        self.assertIn("length and quality are off", out.getvalue())


class ProgressAndResult(Library):

    def test_the_phases_in_order(self):
        jobs, _result, _said = self.rebuild(read=False)
        written = []
        real = update_list.write_progress

        def record(phase, **kwargs):
            written.append((phase, kwargs))
            return real(phase, **kwargs)

        with mock.patch.object(update_list, "write_progress", record), \
                redirect_stdout(io.StringIO()):
            update_list.run_audio_reading(jobs)
        phases = [phase for phase, _kwargs in written]
        self.assertEqual(phases[0], "reading")
        self.assertEqual(written[0][1]["folder_count"], 4)
        self.assertEqual(written[0][1]["folder_index"], 0)
        self.assertLess(phases.index("reading"), phases.index("rewriting"))
        self.assertLess(phases.index("rewriting"), phases.index("publishing"))
        with_rate = [kwargs for phase, kwargs in written if phase == "reading" and "rate" in kwargs]
        self.assertEqual(with_rate[-1]["folder_index"], 4)
        self.assertEqual(phases[-1], "reading", "the swap's phase does not outlive it")
        self.assertFalse(os.path.exists(update_list.progress_path()), "cleared at the end")

    def test_the_progress_file_names_its_writer(self):
        update_list.write_progress("reading", folder_index=3, folder_count=9, rate=7, force=True)
        progress = update_list.read_progress()
        self.assertEqual((progress["phase"], progress["pid"], progress["rate"]),
                         ("reading", os.getpid(), 7))

    def test_the_result_lines(self):
        jobs, _result, _said = self.rebuild(read=False)
        with redirect_stdout(io.StringIO()) as out:
            update_list.run_audio_reading(jobs)
        lines = out.getvalue().splitlines()
        self.assertIn("[AUDIO-INFO] Audio info: reading 4 file(s) in the background, 2 at a time.", lines)
        self.assertTrue(lines[-1].startswith(update_list.AUDIO_RESULT_TAG), lines[-1])
        result = json.loads(lines[-1][len(update_list.AUDIO_RESULT_TAG):])
        self.assertEqual(result["unwritten"], [])
        self.assertEqual((result["outcome"], result["read"], result["unreadable"]), ("done", 4, 1))
        self.assertRegex(lines[-2], r"^\[AUDIO-INFO\] Audio info: done: 3 read, 1 unreadable, "
                                    r"list updated\. Read at [\d,]+ files a second, 2 at a time\.$")

    def test_the_wording(self):
        say = update_list.describe_audio_result
        self.assertEqual(say({"outcome": "nothing"}), "Audio info: nothing new to read.")
        self.assertEqual(say({"outcome": "done", "read": 12000, "unreadable": 20, "updated": ["Main"]}),
                         "Audio info: done: 11,980 read, 20 unreadable, list updated.")
        self.assertEqual(say({"outcome": "done", "read": 3, "unreadable": 0, "updated": ["A", "B"]}),
                         "Audio info: done: 3 read, 0 unreadable, lists updated: A, B.")


if __name__ == "__main__":
    unittest.main()
