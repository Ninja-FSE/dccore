"""hard_bans.txt is parsed once per version of the file, not once per message.

#1131: security.check_user_status() runs for every channel PRIVMSG, before
anything knows whether the line is a command. It opened hard_bans.txt, read
it, and re.escape()d and compiled every pattern again for every one of those
lines. Past about 512 patterns that also overflowed re's own compile cache, so
every pattern was recompiled from scratch on every line: at 2000 bans the IRC
read thread managed about four messages a second.

The parsed rules are now kept, keyed on the file's stat, with two guards
against that key going stale:

  * a version whose mtime is too recent to trust is read again on every
    check (only a changed TEXT is parsed again), so a quick same-size rewrite
    in place - which keeps mtime, size and inode identical - is still seen;
  * db.add_hard_ban() and db.remove_hard_ban() drop the parsed copy
    explicitly after they write, whatever the stat says.

What must NOT change is every ban decision: the first matching pattern in file
order, the three pattern shapes, the fail-open path on a read error. The first
test below compares the new scan with a copy of the old one on varied input.
"""

import builtins
import contextlib
import io
import os
import random
import re
import shutil
import tempfile
import time
import unittest

from tests.support import DCCoreTestCase, silence_debug, no_disk_writes

import announce
import db
import security


def _old_first_match(hard_file, user, hostmask):
    """The scan check_user_status() did before #1131, copied verbatim apart
    from the prints. Returns the first matching pattern, or None."""
    user_lower = user.lower()
    full_mask_lower = f"{user_lower}!{hostmask.lower()}" if hostmask else None
    with open(hard_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            pattern = line.strip().lower()
            if not pattern or pattern.startswith("#"):
                continue
            if security.is_over_broad_hard_ban_pattern(pattern):
                continue
            regex_pattern = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
            is_full_mask = "!" in pattern or "@" in pattern
            is_host_pattern = not is_full_mask and any(
                ch in pattern for ch in ".:")
            if is_full_mask and full_mask_lower:
                candidate = full_mask_lower
            elif is_host_pattern and full_mask_lower and "@" in full_mask_lower:
                candidate = full_mask_lower.split("@", 1)[1]
            else:
                candidate = user_lower
            if re.match(regex_pattern, candidate):
                return pattern
    return None


# Patterns of every shape, plus the awkward ones: over-broad lines, comments,
# regex metacharacters, a backslash, and a NEL (\x85), which str.splitlines()
# would treat as a line break where reading the file line by line does not.
PATTERNS = [
    "lidx_*", "leecher", "LeEcHeR2", "*!*@spammer.example.org",
    "mallory!*@*.example.org", "*.dialup.example.com", "192.168.1.*",
    "2001:db8::*", "*", "***", "*!*@*", "*@*", "*!*@*.*", "# lidx_*",
    "#leecher", "   ", "", "a.b*", "[x]*", "^weird$", "back" + chr(92) + "slash*",
    "nel" + chr(0x85) + "x*", "*@host.example.org", "somebot!*",
    "  padded_*  ", "(group)*", "dave", "*bot",
]
USERS = ["lidx_abc", "LIDX_X", "leecher", "leecher2", "leecherbot", "mallory",
         "dave", "SomeBot", "[x]y", "^weird$", "back" + chr(92) + "slashy",
         "nel" + chr(0x85) + "xz", "(group)1", "otherbot", "a.bc"]
HOSTMASKS = [None, "ident@spammer.example.org", "ident@", "x@1.dialup.example.com",
             "id@192.168.1.5", "id@2001:db8::1", "u@host.example.org",
             "u@mail.example.org", "noat"]


class HardBanCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        security._ban_notified.clear()
        self.addCleanup(security._ban_notified.clear)
        security._hard_bans_missing_warned = False
        self.addCleanup(setattr, security, "_hard_bans_missing_warned", False)
        security.forget_hard_ban_rules()
        self.addCleanup(security.forget_hard_ban_rules)

        self.ban_dir = tempfile.mkdtemp(prefix="dccore-hardbans-")
        self.addCleanup(shutil.rmtree, self.ban_dir, True)
        self.hard_bans = os.path.join(self.ban_dir, "hard_bans.txt")
        self.config.HARD_BANS_FILE = self.hard_bans
        self.config.BANS_FILE = os.path.join(self.ban_dir, "bans.txt")
        no_disk_writes(db)
        self._real_send_debug = announce.send_debug
        self.addCleanup(setattr, announce, "send_debug", self._real_send_debug)
        self.notices = silence_debug(announce)

        # Count how often check_user_status() opens and parses the file. The
        # module-level name shadows the builtin for security.py only.
        self.opens = 0
        self.parses = 0

        def counting_open(path, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(self.hard_bans):
                self.opens += 1
            return builtins.open(path, *args, **kwargs)

        security.open = counting_open
        self.addCleanup(delattr, security, "open")
        real_parse = security._parse_hard_ban_rules

        def counting_parse(*args, **kwargs):
            self.parses += 1
            return real_parse(*args, **kwargs)

        security._parse_hard_ban_rules = counting_parse
        self.addCleanup(setattr, security, "_parse_hard_ban_rules", real_parse)

    def write(self, text):
        """Rewrite the file IN PLACE, the way an editor or a test does."""
        with builtins.open(self.hard_bans, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def age(self, seconds=60):
        """Make the file's mtime old enough that its stat key is trusted."""
        then = time.time_ns() - seconds * 1000 * 1000 * 1000
        os.utime(self.hard_bans, ns=(then, then))

    def check(self, nick, hostmask=None):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = security.check_user_status(nick, hostmask=hostmask)
        self.last_stdout = buffer.getvalue()
        return result


class EveryDecisionIsTheSameAsBefore(HardBanCase):

    def test_the_new_scan_matches_the_old_one_on_varied_files(self):
        rng = random.Random(1131)
        for round_no in range(40):
            chosen = rng.sample(PATTERNS, rng.randint(1, len(PATTERNS)))
            ending = rng.choice(["\n", "\r\n", "\r"])
            self.write(ending.join(chosen) + rng.choice(["", ending]))
            if round_no % 2:
                self.age()
            security.forget_hard_ban_rules()
            for user in USERS:
                for hostmask in HOSTMASKS:
                    with self.subTest(round=round_no, user=user, hostmask=hostmask):
                        expected = _old_first_match(self.hard_bans, user, hostmask)
                        security._ban_notified.clear()
                        allowed = self.check(user, hostmask)
                        self.assertEqual(allowed, expected is None)
                        if expected is not None:
                            self.assertIn(f"matched banned pattern '{expected}'",
                                          self.last_stdout)

    def test_the_first_match_in_file_order_is_the_one_reported(self):
        self.write("lidx_a*\nlidx_*\n")
        self.check("lidx_abc")
        self.assertIn("matched banned pattern 'lidx_a*'", self.last_stdout)
        self.write("lidx_*\nlidx_a*\n")
        self.check("lidx_abc")
        self.assertIn("matched banned pattern 'lidx_*'", self.last_stdout)


class TheFileIsParsedOncePerVersion(HardBanCase):

    def test_an_unchanged_settled_file_is_neither_reopened_nor_reparsed(self):
        self.write("".join(f"pattern{i}_*\n" for i in range(600)) + "lidx_*\n")
        self.age()
        for _ in range(5):
            self.assertFalse(self.check("lidx_abc"))
            self.assertTrue(self.check("dave"))
        self.assertEqual(self.opens, 1)
        self.assertEqual(self.parses, 1)

    def test_a_fresh_file_is_reread_but_parsed_only_when_its_text_changes(self):
        self.write("lidx_*\n")
        for _ in range(4):
            self.assertFalse(self.check("lidx_abc"))
        self.assertEqual(self.opens, 4, "a just-written file must be read again "
                         "on every check until its mtime is old enough to trust")
        self.assertEqual(self.parses, 1)

    def test_the_over_broad_warning_prints_once_per_version(self):
        self.write("*!*@*\nlidx_*\n")
        self.check("dave")
        self.assertIn("SECURITY WARNING", self.last_stdout)
        self.check("dave")
        self.assertNotIn("SECURITY WARNING", self.last_stdout)
        self.write("*!*@*\nleecher\n")
        self.check("dave")
        self.assertIn("SECURITY WARNING", self.last_stdout)

    def test_a_same_size_rewrite_in_place_with_the_same_mtime_is_seen(self):
        """The skeptic's stale key: a quick rewrite in place keeps mtime, size
        and inode identical. While the mtime is recent the key is not trusted,
        so the new text is still read."""
        self.write("aaaa_*\n")
        self.assertFalse(self.check("aaaa_x"))
        before = os.stat(self.hard_bans)
        self.write("bbbb_*\n")
        os.utime(self.hard_bans, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = os.stat(self.hard_bans)
        self.assertEqual((before.st_mtime_ns, before.st_size, before.st_ino),
                         (after.st_mtime_ns, after.st_size, after.st_ino))
        self.assertTrue(self.check("aaaa_x"))
        self.assertFalse(self.check("bbbb_x"))

    def test_a_settled_file_that_changes_is_read_again(self):
        self.write("aaaa_*\n")
        self.age(120)
        self.assertFalse(self.check("aaaa_x"))
        self.write("bbbb_*\n")
        self.age(60)
        self.assertTrue(self.check("aaaa_x"))
        self.assertFalse(self.check("bbbb_x"))
        self.assertEqual(self.parses, 2)


class EveryWriterDropsTheParsedCopy(HardBanCase):

    def test_ban_and_unban_take_effect_on_the_next_message(self):
        self.write("leecher\n")
        self.age()
        self.assertTrue(self.check("lidx_abc"))
        self.assertTrue(db.add_hard_ban("lidx_*"))
        self.assertFalse(self.check("lidx_abc"))
        self.assertTrue(db.remove_hard_ban("lidx_*"))
        self.assertTrue(self.check("lidx_abc"))

    def _writer_drops_cache_even_if_stat_is_unchanged(self, writer, pattern):
        """A writer whose write leaves the stat key exactly as it was must
        still make the next check read the file: the drop is explicit."""
        self.write("leecher\n")
        self.age()
        self.check("dave")
        self.assertIsNotNone(security._hard_ban_cache)
        real_write = db._atomic_write
        self.addCleanup(setattr, db, "_atomic_write", real_write)
        db._atomic_write = lambda path, text, mode=None: None
        writer(pattern)
        self.assertIsNone(security._hard_ban_cache)
        opens = self.opens
        self.check("dave")
        self.assertEqual(self.opens, opens + 1)

    def test_add_hard_ban_drops_the_parsed_copy(self):
        self._writer_drops_cache_even_if_stat_is_unchanged(db.add_hard_ban, "lidx_*")

    def test_remove_hard_ban_drops_the_parsed_copy(self):
        self._writer_drops_cache_even_if_stat_is_unchanged(db.remove_hard_ban, "leecher")

    def test_a_failed_write_still_drops_the_parsed_copy(self):
        self.write("leecher\n")
        self.age()
        self.check("dave")
        real_write = db._atomic_write
        self.addCleanup(setattr, db, "_atomic_write", real_write)

        def failing_write(path, text, mode=None):
            raise OSError("disk full")

        db._atomic_write = failing_write
        with self.assertRaises(OSError):
            db.add_hard_ban("lidx_*")
        self.assertIsNone(security._hard_ban_cache)

        self.check("dave")
        self.assertIsNotNone(security._hard_ban_cache)
        with self.assertRaises(OSError):
            db.remove_hard_ban("leecher")
        self.assertIsNone(security._hard_ban_cache)


class TheFailOpenPathIsUnchanged(HardBanCase):

    def test_a_read_error_fails_open_and_keeps_the_notified_mark(self):
        self.write("lidx_*\n")
        self.assertFalse(self.check("lidx_abc"))
        self.assertIn("lidx_abc", security._ban_notified)
        os.remove(self.hard_bans)
        os.mkdir(self.hard_bans)          # exists, but cannot be read as a file
        self.assertTrue(self.check("lidx_abc"))
        self.assertIn("Could not read", self.last_stdout)
        self.assertNotIn("not being enforced", self.last_stdout)
        self.assertIn("lidx_abc", security._ban_notified,
                      "a failed read is 'unknown', not 'clean'")

    def test_a_missing_file_still_takes_the_missing_path(self):
        self.write("lidx_*\n")
        self.age()
        self.assertFalse(self.check("lidx_abc"))
        os.remove(self.hard_bans)
        self.assertTrue(self.check("lidx_abc"))
        self.assertIn("not being enforced", self.last_stdout)


if __name__ == "__main__":
    unittest.main()
