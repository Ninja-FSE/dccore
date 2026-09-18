"""Resolving a request's folder streams the list; it does not load it.

Found on a live install with 5.4 million files - a 460 MB list - by looking at
the daemon's memory: 1.5 GB resident and a 5.9 GB peak. dcc.handle_download_
request() used to readlines() every published list into one Python list of
strings and then, on a match, walk BACKWARDS through it to the nearest folder
heading. The whole list in memory was for that backward walk and nothing else.

It ran on EVERY file request, because the direct check before it is
"<first folder>/<name>" and a track is never in a folder's root. So every
request cost 460 MB of text as ~5.9 GB of str objects, freed afterwards but
kept by the allocator. Three busy slots could mean three at once.

Headings precede their rows, so "the nearest heading above the matching row"
is the last heading seen on the way down. The lookup now carries that in one
variable and holds nothing. The answer - which folder, which spelling, which
copy under a size hint - is unchanged, and tests/test_download_resolution.py
pins all of that already. This file pins the memory.
"""

import io
import os
import tracemalloc
import unittest

from tests import support  # noqa: F401  (path setup)

import defaults as config  # noqa: E402
import dcc  # noqa: E402

from tests.test_path_security import InlineThread, PathSecurityBase, quiet  # noqa: E402

NAME = "Track 01.flac"
ALBUM = "Some Album"
# Enough rows that materialising them is unmistakable against streaming them.
# ~4 MB of list text; as one list of str objects that is tens of MB, and as a
# stream it is a few KB whatever the file size.
FILLER_ROWS = 60_000


class ALargeListIsStreamed(PathSecurityBase):

    def setUp(self):
        super().setUp()
        directory = os.path.join(self.tree.music, ALBUM)
        os.makedirs(directory, exist_ok=True)
        self.target = os.path.join(directory, NAME)
        with io.open(self.target, "w", encoding="utf-8") as handle:
            handle.write("x")
        self.list_path = self._write_big_list()

    def _write_big_list(self):
        """update_list.py's shape: a heading wrapped in rule lines, then rows.
        The wanted row is LAST, so the lookup has to pass every filler row to
        find it - which is what makes a materialising implementation pay."""
        rule = "=" * 53
        path = os.path.join(self.tree.lists, "%s-2026-01-01.txt" % config.LIST_BASE_NAME)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("List of files generated on Jan 1st\n\n")
            handle.write("\n%s\nD:\\MUSIC\\Filler Album\\\n%s\n" % (rule, rule))
            for i in range(FILLER_ROWS):
                handle.write("!%s Filler Track %06d - Some Rather Long Title.flac  ::INFO:: 9.9MB\n"
                             % (config.NICKNAME, i))
            handle.write("\n%s\nD:\\MUSIC\\%s\\\n%s\n" % (rule, ALBUM, rule))
            handle.write("!%s %s  ::INFO:: 1.0MB\n" % (config.NICKNAME, NAME))
        return path

    def _request(self):
        self.notices.clear()
        InlineThread.dispatched = []
        config.dcc_queue.clear()
        with quiet():
            dcc.handle_download_request(self.sock, "dave", NAME, "#dccore-test")

    def _served_path(self):
        for name, args in InlineThread.dispatched:
            if name == "start_dcc_send":
                return args[2]
        for rows in config.dcc_queue.values():
            for row in rows:
                if not row.get("is_temporary_zip"):
                    return row.get("path")
        return None

    def test_the_file_is_still_found_at_the_end_of_a_large_list(self):
        """The answer first; the memory claim means nothing if this is wrong."""
        self._request()
        self.assertEqual(self._served_path(), self.target)

    def test_resolving_it_does_not_allocate_the_list(self):
        """Measured with tracemalloc, which counts Python allocations only -
        so the file's bytes passing through the read buffer do not register,
        and what shows up is exactly the thing that must not exist: the list
        as a Python object."""
        list_bytes = os.path.getsize(self.list_path)
        self.assertGreater(list_bytes, 3_000_000, "the fixture must be large enough to matter")

        tracemalloc.start()
        try:
            self._request()
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(self._served_path(), self.target)
        # A materialised list of this size is tens of MB of str objects.
        # Streaming it is the read buffer plus a few live strings. The bar is
        # a fifth of the FILE size - generous for a stream, impossible for a
        # copy, whose str overhead alone exceeds the file.
        self.assertLess(peak, list_bytes // 5,
                        f"resolving one request allocated {peak / 1e6:.1f} MB against a "
                        f"{list_bytes / 1e6:.1f} MB list - the list is being loaded, not streamed")

    def test_the_lookup_holds_no_readlines(self):
        """The statement that was the bug, not merely its name."""
        with io.open(os.path.join(os.path.dirname(dcc.__file__), "dcc.py"),
                     encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def handle_download_request("):]
        body = body[:body.index("\ndef ", 10)]
        code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
        self.assertNotIn("readlines()", code)
        self.assertNotIn("range(idx, -1, -1)", code, "the backward walk is what needed the list")


if __name__ == "__main__":
    unittest.main()
