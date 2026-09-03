"""Regression tests for security.check_user_status - ban enforcement.

Ban enforcement used to be a complete no-op: the ``return False`` inside the
hard-ban branch was commented out, so every nick - banned or not - came back
True and the daemon happily served people who had been banned for months.
The fixes since then also had to deal with a second problem: the debug notice
that announces a block calls announce.send_debug, which sleeps 0.5s under a
lock on the IRC read thread. One notice per BLOCKED MESSAGE froze the network
loop, so notices are now suppressed to one per nick per run.

Every test here names the defect it guards against in its docstring.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

from tests.support import DCCoreTestCase, silence_debug, no_disk_writes

import announce
import db
import security


class BanEnforcementTests(DCCoreTestCase):
    """check_user_status must actually deny banned nicks."""

    def setUp(self):
        super().setUp()
        # security._ban_notified is a module global that survives between tests,
        # exactly as it survives between messages in the daemon. Start clean and
        # leave clean, or the "notify once" cases contaminate each other.
        security._ban_notified.clear()
        self.addCleanup(security._ban_notified.clear)

        # Same shape as _ban_notified above: a module global that survives
        # between tests exactly as it survives between messages in the
        # daemon. Start clean and leave clean.
        security._hard_bans_missing_warned = False
        self.addCleanup(setattr, security, "_hard_bans_missing_warned", False)

        self.ban_dir = tempfile.mkdtemp(prefix="dccore-bans-")
        self.addCleanup(shutil.rmtree, self.ban_dir, True)
        self.hard_bans = os.path.join(self.ban_dir, "hard_bans.txt")
        self.config.HARD_BANS_FILE = self.hard_bans
        self.config.BANS_FILE = os.path.join(self.ban_dir, "bans.txt")

        # Never write to the real data/ directory when an expired ban is pruned.
        no_disk_writes(db)
        # send_debug paces itself with a 0.5s sleep; capture it instead.
        self._real_send_debug = announce.send_debug
        self.addCleanup(setattr, announce, "send_debug", self._real_send_debug)
        self.notices = silence_debug(announce)

    # -- helpers ---------------------------------------------------------

    def write_hard_bans(self, *lines):
        """(Re)write the hard-ban file with one pattern per line."""
        with open(self.hard_bans, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def check(self, nick):
        """Call check_user_status with its [SECURITY BLOCK] prints swallowed."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = security.check_user_status(nick)
        self.last_stdout = buffer.getvalue()
        return result

    # -- hard bans -------------------------------------------------------

    def test_hard_ban_pattern_denies(self):
        """Defect: the hard-ban branch's `return False` was commented out, so a
        nick matching a wildcard in hard_bans.txt was still served."""
        self.write_hard_bans("lidx_*")
        self.assertFalse(self.check("lidx_abc"))

    def test_hard_ban_match_is_case_insensitive(self):
        """Defect: a banned nick reconnecting in a different case walked straight
        through. The pattern and the nick are both lowercased before matching."""
        self.write_hard_bans("LiDx_*")
        self.assertFalse(self.check("LIDX_ABC"))
        self.assertFalse(self.check("lidx_abc"))

    def test_exact_pattern_without_wildcard_denies_only_that_nick(self):
        """A pattern with no '*' is anchored: it matches that nick and nothing
        that merely starts with it."""
        self.write_hard_bans("leecher")
        self.assertFalse(self.check("leecher"))
        self.assertTrue(self.check("leecherbot"))

    def test_comment_lines_are_not_patterns(self):
        """Defect: '#'-prefixed documentation lines were compiled as patterns, so
        a commented-out ban still banned."""
        self.write_hard_bans("# lidx_*", "#leecher", "", "   ")
        self.assertTrue(self.check("lidx_abc"))
        self.assertTrue(self.check("leecher"))
        self.assertEqual(self.notices, [])

    def test_star_only_pattern_is_refused(self):
        """Defect: a single '*' line in hard_bans.txt locked out every nick in all
        six channels. Star-only patterns are skipped, and the rest of the file is
        still applied."""
        self.write_hard_bans("*", "***", "lidx_*")
        # The over-broad lines must not catch an unrelated user...
        self.assertTrue(self.check("dave"))
        # ...and the scan must carry on to the legitimate pattern below them.
        self.assertFalse(self.check("lidx_abc"))
        self.assertIn("SECURITY WARNING", self.last_stdout)

    def test_clean_user_is_allowed(self):
        """A nick matching nothing returns True - enforcement must not fail closed."""
        self.write_hard_bans("lidx_*", "# a comment")
        self.assertTrue(self.check("dave"))
        self.assertEqual(self.notices, [])

    def test_missing_hard_ban_file_allows(self):
        """No hard_bans.txt at all is not an error: the scan fails open."""
        self.assertFalse(os.path.exists(self.hard_bans))
        self.assertTrue(self.check("dave"))

    def test_the_file_handle_is_closed_before_the_deny_notice_is_sent(self):
        """#162 finding #25: a match used to be reported (return _deny(...),
        which prints and calls announce.send_debug()) from INSIDE the `with
        open(hard_bans.txt)` block, holding the handle open across that work.
        On Windows, db._atomic_write()'s os.replace() during a concurrent
        !ban/!unban raises PermissionError against any handle still open on
        the same path - so the longer this handle stayed open past the point
        where the match was already known, the likelier a write collided with
        it. The handle must be fully closed before send_debug() (the deny
        notice) is ever called."""
        self.write_hard_bans("lidx_*")

        real_open = open
        opened_handle = {}

        def tracking_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if os.path.abspath(path) == os.path.abspath(self.hard_bans):
                opened_handle["handle"] = handle
            return handle

        closed_at_notify_time = []

        def fake_send_debug(*_a, **_kw):
            handle = opened_handle.get("handle")
            closed_at_notify_time.append(handle.closed if handle else None)

        import builtins
        real_builtin_open = builtins.open
        builtins.open = tracking_open
        self.addCleanup(setattr, builtins, "open", real_builtin_open)
        announce.send_debug = fake_send_debug
        self.addCleanup(setattr, announce, "send_debug", self._real_send_debug)

        result = security.check_user_status("lidx_abc")

        self.assertFalse(result)
        self.assertEqual(closed_at_notify_time, [True],
                         "the hard_bans.txt handle must already be closed by "
                         "the time the deny notice is sent")

    def test_missing_hard_ban_file_warns_once_not_every_message(self):
        """Defect: failing open used to do so silently, every single time this
        runs - which is every message, since this is a hot path. A wrong
        working directory degrades identically to "no hard bans configured",
        so the fix is a warning, not a failure - but one per process, not
        one per message, or the notice becomes the flood it would be warning
        about."""
        self.assertFalse(os.path.exists(self.hard_bans))

        self.check("dave")
        self.assertIn(self.hard_bans, self.last_stdout)
        self.assertIn("not being enforced", self.last_stdout)

        self.check("someoneelse")
        self.assertNotIn(self.hard_bans, self.last_stdout,
                         "warned again on the second message, not just the first")

    # -- timed bans ------------------------------------------------------

    def test_timed_ban_with_future_expiry_denies(self):
        """Defect: timed bans in config.banned_users were regex-matched like
        wildcard lines instead of having their expiry compared, so a live day-ban
        never actually denied."""
        self.config.banned_users["lidx_abc"] = time.time() + 3600
        self.assertFalse(self.check("LIDX_ABC"))
        self.assertEqual([c for c, _ in self.notices], ["TBAN"])
        # Still live, so it must not be pruned.
        self.assertIn("lidx_abc", self.config.banned_users)

    def test_expired_timed_ban_allows_and_is_pruned(self):
        """Defect: an expired day-ban was never removed, so the nick stayed
        blocked past midnight. A past expiry allows the user AND drops the row."""
        self.config.banned_users["lidx_abc"] = time.time() - 10
        self.assertTrue(self.check("lidx_abc"))
        self.assertNotIn("lidx_abc", self.config.banned_users)
        self.assertEqual(self.notices, [])

    def test_corrupt_timed_ban_expiry_is_treated_as_expired(self):
        """A non-numeric expiry (a hand-edited bans.txt) must not raise on the IRC
        read thread; it is treated as long expired and pruned."""
        self.config.banned_users["lidx_abc"] = "not-a-timestamp"
        self.assertTrue(self.check("lidx_abc"))
        self.assertNotIn("lidx_abc", self.config.banned_users)

    # -- the debug-notice rate limit -------------------------------------

    def test_debug_notice_fires_once_per_nick(self):
        """Defect: every blocked message sent its own debug notice, and
        send_debug sleeps 0.5s under a lock on the IRC read thread - a flooding
        banned nick stalled the network loop into a ping timeout."""
        self.write_hard_bans("lidx_*")
        for _ in range(6):
            self.assertFalse(self.check("lidx_abc"))
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.notices[0][0], "BAN")

    def test_debug_notice_is_per_nick_not_global(self):
        """The one-notice rule is keyed on the nick: a second banned nick still
        gets its own notice."""
        self.write_hard_bans("lidx_*", "leecher")
        self.assertFalse(self.check("lidx_abc"))
        self.assertFalse(self.check("leecher"))
        self.assertFalse(self.check("lidx_abc"))
        self.assertEqual(len(self.notices), 2)

    def test_notify_mark_cleared_when_user_seen_clean(self):
        """Defect: the 'already notified' mark was only cleared on the timed-ban
        expiry path, so after an !unban and a later re-ban the debug channel
        stayed silent and the admin never saw the pattern take effect."""
        self.write_hard_bans("lidx_*")
        self.assertFalse(self.check("lidx_abc"))
        self.assertEqual(len(self.notices), 1)

        # !unban rewrites the file without the pattern; the nick is seen clean.
        self.write_hard_bans("# unbanned")
        self.assertTrue(self.check("lidx_abc"))
        self.assertNotIn("lidx_abc", security._ban_notified)

        # A later re-ban must notify once more.
        self.write_hard_bans("lidx_*")
        self.assertFalse(self.check("lidx_abc"))
        self.assertEqual(len(self.notices), 2)

    def test_notify_mark_survives_an_unreadable_ban_file(self):
        """Defect: clearing the mark on the fail-open path re-armed the notice.
        A missing file - or a read landing inside the truncate window of an
        !unban rewrite - is 'unknown', not 'clean', so the mark must survive and
        the next block on the same nick must stay silent."""
        self.write_hard_bans("lidx_*")
        self.assertFalse(self.check("lidx_abc"))
        self.assertEqual(len(self.notices), 1)

        # The file vanishes mid-run: the scan fails open, but nothing was proven
        # clean, so the mark must NOT be dropped.
        os.remove(self.hard_bans)
        self.assertTrue(self.check("lidx_abc"))
        self.assertIn("lidx_abc", security._ban_notified)

        # The file comes back with the pattern intact - still no second notice.
        self.write_hard_bans("lidx_*")
        self.assertFalse(self.check("lidx_abc"))
        self.assertEqual(len(self.notices), 1)


class HostmaskBanPatternTests(BanEnforcementTests):
    """#162 finding #3: a nick can never contain "!" or "@", so a pattern
    written in hostmask form - exactly what adminchat.py's own !ban usage
    line has always told operators to type, e.g. "*!*@spammer.net" - could
    never match check_user_status(nick)'s bare-nick-only comparison. It was
    accepted, written to hard_bans.txt, confirmed, and listed forever by
    the console - and matched nothing, ever.

    Reuses BanEnforcementTests' setUp/write_hard_bans exactly - only the
    call under test differs, so this subclasses it rather than duplicating
    the fixture."""

    def check_with_host(self, nick, hostmask):
        """Same as check(), but also supplies the sender's ident@host, the
        way irc.py's PRIVMSG dispatch now does via parse_privmsg()."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = security.check_user_status(nick, hostmask=hostmask)
        self.last_stdout = buffer.getvalue()
        return result

    def test_a_hostmask_pattern_denies_when_the_host_is_supplied(self):
        """The exact usage-line example, working end to end."""
        self.write_hard_bans("*!*@spammer.net")
        self.assertFalse(self.check_with_host("mallory", "ident@spammer.net"))

    def test_a_hostmask_pattern_is_case_insensitive(self):
        self.write_hard_bans("*!*@SPAMMER.NET")
        self.assertFalse(self.check_with_host("mallory", "ident@spammer.net"))

    def test_a_hostmask_pattern_can_also_pin_the_nick(self):
        self.write_hard_bans("mallory!*@spammer.net")
        self.assertFalse(self.check_with_host("mallory", "ident@spammer.net"))
        self.assertTrue(self.check_with_host("someoneelse", "ident@spammer.net"),
                        "the nick half of the mask must still be honoured")

    def test_a_hostmask_pattern_without_a_host_still_denies_nothing(self):
        """Without `hostmask` (an older/other caller), a hostmask-shaped
        pattern falls back to nick-only matching - same as before this
        parameter existed - so it correctly matches nobody rather than
        raising."""
        self.write_hard_bans("*!*@spammer.net")
        self.assertTrue(self.check("mallory"))

    def test_a_clean_host_is_not_denied_by_an_unrelated_hostmask_pattern(self):
        self.write_hard_bans("*!*@spammer.net")
        self.assertTrue(self.check_with_host("dave", "ident@example.com"))

    def test_a_plain_nick_pattern_still_matches_the_bare_nick_with_a_host_present(self):
        """Supplying a host must not stop an ordinary nick-only pattern
        from working exactly as it always has."""
        self.write_hard_bans("lidx_*")
        self.assertFalse(self.check_with_host("lidx_abc", "ident@example.com"))


if __name__ == "__main__":
    unittest.main()


class TheNotifiedNickSetIsBounded(unittest.TestCase):
    """security._ban_notified used to be a plain set() that only ever grew.

    Its two removal paths - a timed ban expiring, and the nick later being seen
    clean - are both unreachable for a nick matched by a WILDCARD pattern in
    hard_bans.txt, because the "seen clean" branch only runs when the user is
    not banned. An operator with any wildcard entry, plus somebody cycling
    nicks against it, grew the set for the life of the process.
    """

    def test_the_old_shape_would_have_grown_without_limit(self):
        """Control. A plain set is what this replaced; if THIS ever stops
        growing, the premise below is wrong."""
        plain = set()
        for i in range(20000):
            plain.add(f"leech{i}")
        self.assertEqual(len(plain), 20000)

    def test_entries_outside_the_window_are_swept_away(self):
        """The bound is RATE x WINDOW, not an absolute cap - so what has to be
        true is that everything older than the window goes."""
        seen = security._NotifiedNicks(ttl=0.05, sweep_every=0.0)
        for i in range(2000):
            seen.add(f"leech{i}")
        self.assertGreater(len(seen), 0)
        time.sleep(0.08)
        seen.add("one-more")             # any add triggers the sweep
        self.assertEqual(len(seen), 1,
                         f"{len(seen)} stale entries survived the sweep")

    def test_a_nick_is_forgotten_once_the_window_passes(self):
        seen = security._NotifiedNicks(ttl=0.05)
        seen.add("dave")
        self.assertIn("dave", seen)
        time.sleep(0.08)
        self.assertNotIn("dave", seen,
                         "the entry should have expired and been forgotten")

    def test_a_nick_inside_the_window_is_still_suppressed(self):
        """The whole point of the structure. send_debug sleeps 0.5s holding a
        lock on the IRC reader thread, so re-notifying a nick that is still
        being denied is what freezes the network loop."""
        seen = security._NotifiedNicks(ttl=3600.0)
        seen.add("dave")
        for _ in range(100):
            self.assertIn("dave", seen)

    def test_cycling_nicks_cannot_force_a_recent_nick_to_be_re_notified(self):
        """The reason this expires by TIME rather than by size.

        A plain LRU would evict the oldest entry whenever the cap was reached,
        so an attacker cycling through more nicks than the cap would push a
        recently-notified nick out and earn a fresh 0.5s notice for it - turning
        a slow memory leak into the exact read-thread freeze the suppression
        exists to prevent. Eviction driven by the clock has no such edge.
        """
        seen = security._NotifiedNicks(ttl=3600.0)
        seen.add("victim")
        for i in range(5000):            # far more than the cap
            seen.add(f"attacker{i}")
        self.assertIn("victim", seen,
                      "a recently-notified nick was evicted by nick cycling, "
                      "so it would be re-notified and stall the read thread")

    def test_discard_and_clear_still_behave_like_the_set_they_replaced(self):
        seen = security._NotifiedNicks()
        seen.add("dave")
        seen.discard("dave")
        self.assertNotIn("dave", seen)
        seen.discard("never-added")      # must not raise, like set.discard
        seen.add("erik")
        seen.clear()
        self.assertNotIn("erik", seen)
        self.assertEqual(len(seen), 0)

    def test_it_survives_concurrent_use(self):
        """check_user_status runs on the IRC reader thread while commands can
        touch the same structure; pruning must not trip over a concurrent
        write."""
        seen = security._NotifiedNicks(ttl=3600.0)
        errors = []

        def hammer(base):
            try:
                for i in range(2000):
                    seen.add(f"{base}{i}")
                    _ = f"{base}{i}" in seen
                    seen.discard(f"{base}{i - 50}")
            except Exception as err:      # pragma: no cover
                errors.append(err)

        threads = [threading.Thread(target=hammer, args=(f"t{n}-",))
                   for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertFalse([t for t in threads if t.is_alive()], "a worker hung")
