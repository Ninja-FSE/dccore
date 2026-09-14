"""One real `!rehash`, executed in a booted process.

WHY THIS DID NOT EXIST

`_handle_rehash_request()` is roughly seven hundred lines that quiesce
transfers, `importlib.reload()` eight modules, merge runtime state back,
re-baseline the nick, restore the advert token, reattach debug sinks and sync
channels. Across 120-odd test files, nothing ran it. Every existing test
either stubs `commands._handle_rehash_request`, calls
`reload_modules_in_order(modules=('defaults',))` for one harmless module, or
reads the function's source as text. test_commands.py says why outright:
running the real reload "would risk the identical thing happening to test
state".

That is the correct call for a unit suite and the wrong place to leave it for
a release. This is not a rare admin command - `webserver.py` dispatches it on
EVERY dashboard settings save, and `adminchat.py` and `irc.py` are two more
entry points. The daemon's least-tested code path is the one an operator
triggers by ticking a checkbox, and three of the audit's confirmed findings
lived inside it.

HOW IT RUNS ANYWAY

In a subprocess, against a throwaway working directory. The objection is
entirely about reloading modules the test runner is itself holding - a
separate interpreter has no such problem. The child seeds the live state a
rehash exists to carry across, changes settings.conf underneath itself the way
a dashboard save does, runs the real thing, and reports what survived as JSON.

WHAT THE FIXTURE HAD TO LEARN

The first version assigned `config.dcc_queue = {...}` and the queue came back
empty - which reads as the rehash losing it. It is not: dcc_queue is a
runtime.py-bound container and runtime is not reloaded, which is precisely why
it survives. Rebinding it on `config` detaches that alias, and the reload
re-points config at the runtime object. defaults.py says so in as many words
- "Mutate them in place. Never rebind them" - so the fixture was breaking the
documented invariant, not finding a defect. It mutates now, as the daemon
does.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRIVER = '''
import io, json, os, sys, tempfile, time

REPO, OUT = sys.argv[1], sys.argv[2]
work = tempfile.mkdtemp(prefix="rehash-e2e-")

conf = os.path.join(work, "settings.conf")
io.open(conf, "w", encoding="utf-8").write(
    "NICKNAME = RehashBot\\nCHANNEL = #one,#two\\nMAX_DCC_SLOTS = 4\\n")
os.environ["DCCORE_SETTINGS_FILE"] = conf
os.makedirs(os.path.join(work, "data"), exist_ok=True)
os.chdir(work)
sys.path.insert(0, REPO)

result = {"stage": "import"}
try:
    import defaults as config
    import announce
    import commands

    for name in ("BANS_FILE", "STATS_FILE", "DCC_QUEUE_FILE", "KNOWN_BOTS_FILE",
                 "FETCH_HISTORY_FILE", "FETCHED_BOT_LISTS_FILE",
                 "SPEED_RECORD_FILE", "LIST_INDEX_FILE", "LISTS_FILE",
                 "LIBRARY_FOLDERS_FILE", "ON_CONNECT_FILE",
                 "DOWNLOAD_COUNTS_FILE"):
        setattr(config, name, os.path.join(work, "data", name.lower()))

    result["stage"] = "seed"

    class FakeSocket:
        def __init__(self):
            self.sent = []
        def sendall(self, payload):
            return self.send(payload)
        def send(self, payload):
            self.sent.append(payload.decode("utf-8", "replace")
                             if isinstance(payload, bytes) else str(payload))
            return len(payload)

    sock = FakeSocket()
    oserve = type(sys)("oserve")
    oserve.irc_connection = sock

    # THE REAL MODULE'S QUEUE ENTRY POINT, because the rehash now uses it.
    # The channel sync writes its JOIN/PART/NAMES through oserve.queue_message
    # rather than to the socket (#440), so a stand-in carrying only
    # irc_connection takes the whole rehash down with an AttributeError -
    # which is how this fixture met that change, and is the argument for the
    # fixture existing.
    oserve.queued = []

    def queue_message(user, message, is_vip=False):
        oserve.queued.append(message)

    oserve.queue_message = queue_message
    sys.modules["oserve"] = oserve

    served = os.path.join(work, "music", "a.flac")
    os.makedirs(os.path.dirname(served), exist_ok=True)
    io.open(served, "wb").write(b"x" * 4096)

    # MUTATED, never rebound - these are runtime.py-bound containers and that
    # is why they survive a reload at all.
    config.dcc_queue.clear()
    config.dcc_queue["alice"] = [{"path": served, "file": "a.flac", "size": 4096}]
    config.channel_users.clear()
    config.channel_users.update({"#one": {"alice", "bob"}, "#two": {"carol"}})
    config.frozen_queues.clear()
    config.frozen_queues["dave"] = time.time()
    config.banned_users.clear()
    config.banned_users["flooder"] = time.time() + 3600

    config.active_transfers = []
    config.rar_inprogress = False
    config.transfers_paused = False
    config.bot_joined_channel = True
    config.ADMIN_NICK = "admin"
    announce.current_worker_id = 4242
    announce.is_ready = True

    # settings.conf changes under the running bot: the ordinary reason to
    # rehash, and exactly what a dashboard save does before triggering one.
    io.open(conf, "w", encoding="utf-8").write(
        "NICKNAME = RehashBot\\nCHANNEL = #one,#two\\nMAX_DCC_SLOTS = 9\\n")

    result["stage"] = "rehash"
    commands.handle_rehash_request("admin", "#one", authorised=True)

    result["stage"] = "collect"
    import defaults as after
    result.update(
        ok=True,
        slots_after=getattr(after, "MAX_DCC_SLOTS", None),
        queue_users=sorted(getattr(after, "dcc_queue", {})),
        queue_files=[row.get("file") for row in
                     getattr(after, "dcc_queue", {}).get("alice", [])],
        channels=sorted(getattr(after, "channel_users", {})),
        channel_one=sorted(getattr(after, "channel_users", {}).get("#one", [])),
        frozen=sorted(getattr(after, "frozen_queues", {})),
        banned=sorted(getattr(after, "banned_users", {})),
        worker_id=getattr(sys.modules["announce"], "current_worker_id", None),
        paused_after=bool(getattr(after, "transfers_paused", False)),
        announce_ready=bool(getattr(sys.modules["announce"], "is_ready", False)),
        sync_queued=list(getattr(oserve, "queued", [])),
        socket_lines=list(sock.sent),
    )
except BaseException as err:
    import traceback
    result.update(ok=False, error=str(err)[:400],
                  traceback=traceback.format_exc()[-3000:])

io.open(OUT, "w", encoding="utf-8").write(json.dumps(result))
'''


class ARealRehash(unittest.TestCase):
    """Run once for the whole class - it boots an interpreter and reloads
    eight modules, so paying that per test would be wasteful and would tell us
    nothing extra."""

    result = None
    reason = None

    @classmethod
    def setUpClass(cls):
        work = tempfile.mkdtemp(prefix="rehash-e2e-parent-")
        driver = os.path.join(work, "driver.py")
        out = os.path.join(work, "result.json")
        with io.open(driver, "w", encoding="utf-8") as handle:
            handle.write(DRIVER)

        try:
            completed = subprocess.run(
                [sys.executable, "-B", driver, REPO_ROOT, out],
                capture_output=True, text=True, errors="replace", timeout=300,
                stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as err:
            cls.reason = f"could not run the rehash driver: {err}"
            return

        if not os.path.exists(out):
            cls.reason = ("the driver produced no result; last output: "
                          + (completed.stdout or completed.stderr or "")[-400:])
            return
        with io.open(out, encoding="utf-8") as handle:
            cls.result = json.load(handle)
        cls.output = completed.stdout

    def setUp(self):
        if self.reason:
            self.skipTest(self.reason)

    def test_it_completes_without_raising(self):
        """The headline. Seven hundred lines nothing had ever executed."""
        self.assertTrue(self.result.get("ok"),
                        self.result.get("traceback", self.result.get("error")))
        self.assertEqual(self.result.get("stage"), "collect")

    def test_the_channel_sync_goes_through_the_queue(self):
        """#440, driven rather than unit-tested: these three lines were the
        only outbound path in the daemon that both scaled with the channel
        count and ignored the pacer, and reaching them for real means running
        the whole seven-hundred-line reload."""
        queued = self.result.get("sync_queued") or []

        self.assertTrue(any(line.startswith("NAMES ") for line in queued),
                        queued)

    def test_the_channel_sync_writes_nothing_to_the_socket(self):
        """A line written straight to the socket has taken no pacer slot."""
        for line in self.result.get("socket_lines") or []:
            with self.subTest(line=line[:40]):
                self.assertFalse(line.startswith(("JOIN ", "PART ", "NAMES ")),
                                 line)

    def test_both_channels_share_one_names_line(self):
        """The batching, end to end: two channels, one line, the way irc.py's
        connect path has always joined them."""
        names = [line for line in self.result.get("sync_queued") or []
                 if line.startswith("NAMES ")]

        self.assertEqual(len(names), 1, names)
        self.assertEqual(sorted(names[0][len("NAMES "):].strip().split(",")),
                         ["#one", "#two"])

    def test_the_changed_setting_takes_effect(self):
        """What an operator rehashes FOR. settings.conf went from 4 slots to 9
        while the bot was running."""
        self.assertEqual(self.result.get("slots_after"), 9)

    def test_a_users_queue_survives(self):
        """dcc_queue is runtime-bound and must come through untouched - the
        whole PRESERVE_RUNTIME design rests on this."""
        self.assertEqual(self.result.get("queue_users"), ["alice"])
        self.assertEqual(self.result.get("queue_files"), ["a.flac"])

    def test_the_channel_lists_survive(self):
        """channel_users is what dcc.py reads as proof a user is present. An
        emptied one freezes every queue, which is the failure
        handle_rehash_request's own docstring describes."""
        self.assertEqual(self.result.get("channels"), ["#one", "#two"])
        self.assertEqual(self.result.get("channel_one"), ["alice", "bob"])

    def test_a_timed_ban_is_not_released(self):
        """PRESERVE_RUNTIME's first listed reason: losing banned_users
        silently releases every timed ban.

        TWO INDEPENDENT MECHANISMS KEEP THIS TRUE, which mutation testing is
        what revealed. defaults.py binds the name to runtime.banned_users and
        runtime is not reloaded, so the data survives on its own; and the
        rehash separately snapshots and restores every PRESERVE_RUNTIME name.
        Breaking either one alone leaves this test passing - only breaking
        both fails it, which is exactly what belt-and-braces should look like
        and is worth knowing before anybody decides one of them is redundant.
        """
        self.assertEqual(self.result.get("banned"), ["flooder"])

    def test_a_freeze_timer_survives(self):
        """Losing frozen_queues means a departed user's queue never expires.
        Same pair of mechanisms as the ban above."""
        self.assertEqual(self.result.get("frozen"), ["dave"])

    def test_the_advert_worker_token_survives(self):
        """Zeroed by the reload; restored immediately after it. A worker waking
        in that window retires, leaving the channels silent until the next
        reconnect."""
        self.assertEqual(self.result.get("worker_id"), 4242)

    def test_the_bot_is_not_left_paused(self):
        """The quiesce must not outlive the rehash. A pause that survives it
        leaves the bot refusing every send for ever, with the only clue a
        notice telling users to try again in a moment."""
        self.assertFalse(self.result.get("paused_after"))

    def test_adverts_are_running_again(self):
        self.assertTrue(self.result.get("announce_ready"))

    def test_the_queue_was_woken(self):
        """The rehash resumes sends and then wakes the dispatcher; without the
        wake, queued users wait for a trigger that may never come."""
        self.assertIn("[REHASH-WAKE]", getattr(self, "output", ""))

    def test_it_reported_success_rather_than_failing_quietly(self):
        self.assertIn("[REHASH SUCCESS]", getattr(self, "output", ""))
        self.assertNotIn("[REHASH CRITICAL ERROR]", getattr(self, "output", ""))


if __name__ == "__main__":
    unittest.main()
